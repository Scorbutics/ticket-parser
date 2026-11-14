"""The data structures the PDF is rendered from.

`ChatMessage.text` is the product owner's wording, read from the exported
message.json and never rewritten. (The one person who may change it is you,
by editing the Markdown export before importing it; that is deliberate. A
document part's text is a verbatim slice of the attached document.) Nothing downstream -- clustering, the repo
pass, the template -- may modify it. That is what makes the verbatim guarantee
structural rather than a prompt instruction: the model's replies are consumed
only as ids and labels, so there is no path by which a model could alter text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# A figure inside a document's text: a line "[image: <file name>]", the file
# sitting in the message's directory (ChatMessage.asset_dir). Kept as a line of
# the text so it travels with whichever document part it falls in.
IMAGE_TOKEN = re.compile(r"^\[image: ([^\]\n/\\]+)\]$", re.MULTILINE)


@dataclass(frozen=True)
class Media:
    filename: str
    mimetype: str
    path: Path

    @property
    def is_image(self) -> bool:
        return self.mimetype.startswith("image/")

    @property
    def is_audio(self) -> bool:
        return self.mimetype.startswith("audio/")


@dataclass
class ChatMessage:
    """One WhatsApp message. `seq` is the stable integer id shown to the model."""

    seq: int
    wa_id: str
    chat_id: str
    author_name: str
    timestamp: int
    text: str
    media: Media | None = None
    media_error: str = ""  # a picture existed but could not be retrieved
    document_name: str = ""  # a file was attached; WhatsApp only decrypts on click
    document_text: str = ""  # its text, when it was downloaded and readable (docx, pdf…)
    # A reviewer's comment added while editing the Markdown export. Kept apart
    # from `text`, which is what the sender actually wrote.
    note: str = ""
    # Documents are split into parts that cluster like messages (documents.py).
    # On the carrying message: how many parts its document became. On a part:
    # the carrying message's wa_id, and which part this is.
    document_parts: int = 0
    document_part_of: str = ""
    part_index: int = 0
    part_count: int = 0
    # Where a document's figures live. The message's own directory; for a
    # document part, its carrying message's (a part has no directory of its own).
    asset_dir: Path | None = None
    transcript: str = ""  # speech-to-text of a voice note, filled in after collection
    transcript_error: str = ""  # a voice note existed but could not be transcribed
    # True when the time came from a bubble label rather than full metadata
    # (media rows in the browser extension carry no date).
    timestamp_inferred: bool = False
    source_dir: Path | None = None

    @property
    def sent_at(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc).astimezone()

    def image_path(self, name: str) -> Path | None:
        """Absolute path of a figure named in an [image: …] line, if present."""
        base = self.asset_dir or self.source_dir
        if base is None:
            return None
        path = base / name
        return path if path.is_file() else None

    def segments(self, text: str) -> list[tuple[str, str]]:
        """Text split around its [image: …] lines: [("text", …) | ("image", name)]."""
        out: list[tuple[str, str]] = []
        last = 0
        for match in IMAGE_TOKEN.finditer(text):
            chunk = text[last : match.start()].strip("\n")
            if chunk.strip():
                out.append(("text", chunk))
            out.append(("image", match.group(1)))
            last = match.end()
        tail = text[last:].strip("\n")
        if tail.strip():
            out.append(("text", tail))
        return out

    @property
    def is_document_part(self) -> bool:
        return bool(self.document_part_of)

    @property
    def is_dated(self) -> bool:
        # 0 is what the extension writes when it found no date for the row.
        return self.timestamp > 0

    @property
    def display_time(self) -> str:
        if not self.is_dated:
            return "date unknown"
        return self.sent_at.strftime("%d/%m/%Y %H:%M")

    def fingerprint(self) -> str:
        from .db import fingerprint_chat_message

        return fingerprint_chat_message(
            str(self.timestamp),
            self.author_name,
            self.text,
            # Documents carry no Media (WhatsApp only decrypts on click), so
            # fall back to the filename -- otherwise two documents sent in the
            # same minute with no caption fingerprint identically and the
            # second is silently dropped as already-seen.
            self.media.filename if self.media else self.document_name,
        )


@dataclass
class Evidence:
    file: str
    line: int | None = None
    note: str = ""

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.line else self.file


@dataclass
class RepoVerdict:
    """Result of confronting one issue with the repository."""

    verdict: str = "not_analysed"  # confirmed | not_found | inconclusive | not_analysed
    confidence: str = ""  # low | medium | high
    observations: str = ""
    suggested_fix: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    repo_head: str = ""
    error: str = ""

    VERDICT_LABELS = {
        "confirmed": "Found in the code",
        "not_found": "Not found in the code",
        "inconclusive": "Inconclusive",
        "not_analysed": "Not analysed",
    }

    @property
    def label(self) -> str:
        return self.VERDICT_LABELS.get(self.verdict, self.verdict)

    @property
    def ran(self) -> bool:
        return self.verdict != "not_analysed"


@dataclass
class Issue:
    """One bug, one page of the PDF."""

    key: str
    title: str
    category: str = "uncategorised"
    severity: str = "unknown"
    messages: list[ChatMessage] = field(default_factory=list)
    verdict: RepoVerdict = field(default_factory=RepoVerdict)

    @property
    def started_at(self) -> datetime | None:
        return min((m.sent_at for m in self.messages if m.is_dated), default=None)

    def content_hash(self) -> str:
        """Identity of the issue's *content*, for the verdict cache key."""
        import hashlib

        payload = "\x1f".join(
            [
                self.title,
                *(f"{m.text}\x1e{m.note}\x1e{m.document_text}" for m in self.messages),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Batch:
    """A quiet-period-delimited burst of messages: one PDF."""

    batch_id: str
    messages: list[ChatMessage] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ordering_inferred: bool = False

    @property
    def started_at(self) -> datetime | None:
        return min((m.sent_at for m in self.messages if m.is_dated), default=None)

    @property
    def ended_at(self) -> datetime | None:
        return max((m.sent_at for m in self.messages if m.is_dated), default=None)
