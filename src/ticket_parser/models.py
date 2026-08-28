"""The data structures the PDF is rendered from.

`ChatMessage.text` is the product owner's wording, read from the exported
message.json and never rewritten. Nothing downstream -- clustering, the repo
pass, the template -- may modify it. That is what makes the verbatim guarantee
structural rather than a prompt instruction: the model's replies are consumed
only as ids and labels, so there is no path by which a model could alter text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


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
    transcript: str = ""  # speech-to-text of a voice note, filled in after collection
    transcript_error: str = ""  # a voice note existed but could not be transcribed
    # True when the time came from a bubble label rather than full metadata
    # (media rows in the browser extension carry no date).
    timestamp_inferred: bool = False
    source_dir: Path | None = None

    @property
    def sent_at(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc).astimezone()

    @property
    def display_time(self) -> str:
        return self.sent_at.strftime("%d/%m/%Y %H:%M")

    def fingerprint(self) -> str:
        from .db import fingerprint_chat_message

        return fingerprint_chat_message(
            str(self.timestamp),
            self.author_name,
            self.text,
            self.media.filename if self.media else "",
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
        return min((m.sent_at for m in self.messages), default=None)

    def content_hash(self) -> str:
        """Identity of the issue's *content*, for the verdict cache key."""
        import hashlib

        payload = "\x1f".join([self.title, *(m.text for m in self.messages)])
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
        return min((m.sent_at for m in self.messages), default=None)

    @property
    def ended_at(self) -> datetime | None:
        return max((m.sent_at for m in self.messages), default=None)
