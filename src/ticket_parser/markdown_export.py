"""Read the editable Markdown export written by the Firefox extension.

Layout of one export (a folder, or the same thing zipped)::

    whatsapp-20260115T0912-exported-20260115T1830/
      conversation.md
      media/<id>.jpg, media/<id>.ogg, media/<id>-<document name>

The format is defined by extension/export-format.js; this module is its exact
reverse. A person may have edited the file before it gets here, so every edit
it allows is honoured:

- a deleted message section is simply not imported;
- edited text is taken as the message text;
- a ``> Note: …`` blockquote becomes the message's ``note`` (kept apart from
  what was actually said);
- a section with no ``<!-- msg … -->`` marker becomes a note on the message
  before it (or after it, when it comes first);
- the ```` ```document ```` fence holds the attached document's text.
"""

from __future__ import annotations

import json
import mimetypes
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

CONVERSATION_FILE = "conversation.md"

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_MARKER = re.compile(r"^<!--\s*msg\s+(\{.*\})\s*-->\s*$")
_MEDIA_LINK = re.compile(r"^!?\[([^\]]*)\]\((media/[^)\s]+)\)\s*$")
_FENCE_OPEN = re.compile(r"^(`{3,})document\s*$")
_NOTE_START = re.compile(r"^>\s?(?:📝\s*)?note\s*:\s?(.*)$", re.IGNORECASE)
_WARNING = re.compile(r"^⚠️\s?(.*)$")
_COMMENT_BLOCK = re.compile(r"<!--(?!\s*msg\s).*?-->", re.DOTALL)
# Lines export-format.js escaped with one backslash because they would
# otherwise read as structure.
_ESCAPED = re.compile(r"^\\(#|> ?note:|`{3}|⚠️|<!--|!\[|\[🎤|\[📄|\\)", re.IGNORECASE)

DOCUMENT_EXTENSIONS = {
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf", ".odt", ".ods",
    ".odp", ".rtf", ".zip", ".rar", ".7z", ".csv", ".txt", ".md", ".log",
    ".json", ".xml", ".apk",
}


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _is_figure(line: str, link: re.Match) -> bool:
    """`![📄 figure 2](media/…png)`: a picture from inside a document.

    Told apart from a WhatsApp screenshot (`![screenshot](…)`) by the 📄.
    """
    return (
        line.startswith("!")
        and link.group(1).startswith("📄")
        and Path(link.group(2)).suffix.lower() in IMAGE_SUFFIXES
    )


class MarkdownExportError(ValueError):
    pass


@dataclass
class ExportedMessage:
    wa_id: str
    chat_id: str
    author_name: str
    timestamp: int
    timestamp_inferred: bool
    from_me: bool
    text: str = ""
    note: str = ""
    media_path: str = ""  # image or voice note, relative to the export root
    document_path: str = ""
    document_label: str = ""  # the real file name, from the link text
    # What the page showed about a document it could not include (name, type,
    # size) -- used to find the file on disk instead.
    document_hint: dict = field(default_factory=dict)
    # media/ paths of the pictures from inside the document; their places in
    # document_text are "[image: <file name>]" lines.
    document_figures: list[str] = field(default_factory=list)
    document_text: str = ""
    media_error: str = ""

    def record(self) -> dict:
        """The message.json shape the rest of the pipeline reads."""
        return {
            "schema": 1,
            "id": self.wa_id,
            "chat_id": self.chat_id,
            "author_name": self.author_name,
            "timestamp": self.timestamp,
            "timestamp_inferred": self.timestamp_inferred,
            "from_me": self.from_me,
            "text": self.text,
            "note": self.note,
            "document_name": self.document_label
            or _document_display_name(self.document_path)
            or str(self.document_hint.get("name") or ""),
            "document_text": self.document_text,
            "media_error": self.media_error,
            "source": "markdown-export",
        }


@dataclass
class MarkdownExport:
    front_matter: dict[str, str]
    messages: list[ExportedMessage] = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.front_matter.get("status", "").strip().lower()

    @property
    def ready(self) -> bool:
        return self.status == "ready"


def _document_display_name(path: str) -> str:
    """media/3EB0AB-Rapport_final.pdf -> Rapport_final.pdf"""
    if not path:
        return ""
    name = Path(path).name
    return name.split("-", 1)[1] if "-" in name else name


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key.strip().lower()] = value.strip()
    return values, text[match.end():]


def looks_like_export(path: Path) -> bool:
    try:
        head = path.read_text(encoding="utf-8")[:400]
    except (OSError, UnicodeDecodeError):
        return False
    front, _ = _parse_front_matter(head + ("\n" if not head.endswith("\n") else ""))
    return "ticket-parser" in front


def _split_sections(body: str) -> list[list[str]]:
    sections: list[list[str]] = []
    current: list[str] | None = None
    in_fence = ""
    for line in body.split("\n"):
        if in_fence:
            if line.strip() == in_fence:
                in_fence = ""
        else:
            opened = _FENCE_OPEN.match(line)
            if opened:
                in_fence = opened.group(1)
            elif line.startswith("## "):
                current = [line]
                sections.append(current)
                continue
        if current is not None:
            current.append(line)
    return sections


def _parse_section(lines: list[str]) -> tuple[dict | None, ExportedMessage | None, str]:
    """Returns (marker, message, free_text) for one '## ' section."""
    heading = lines[0][3:].strip()
    marker = None
    text_lines: list[str] = []
    notes: list[str] = []
    document_parts: list[str] = []
    figures: list[str] = []
    warnings: list[str] = []
    media_path = ""
    document_path = ""
    document_label = ""

    index = 1
    while index < len(lines):
        line = lines[index]
        index += 1

        found = _MARKER.match(line)
        if found and marker is None:
            try:
                marker = json.loads(found.group(1))
            except json.JSONDecodeError as exc:
                raise MarkdownExportError(f"unreadable message marker under '{heading}': {exc}") from exc
            continue

        fence = _FENCE_OPEN.match(line)
        if fence:
            body: list[str] = []
            while index < len(lines) and lines[index].strip() != fence.group(1):
                body.append(lines[index])
                index += 1
            index += 1  # the closing fence
            document_parts.append("\n".join(body).strip())
            continue

        note = _NOTE_START.match(line)
        if note:
            block = [note.group(1)]
            while index < len(lines) and lines[index].startswith(">"):
                block.append(re.sub(r"^>\s?", "", lines[index]))
                index += 1
            notes.append("\n".join(block).strip())
            continue

        link = _MEDIA_LINK.match(line)
        if link and _is_figure(line, link):
            # A picture from inside the attached document, at its place in
            # the document's text.
            document_parts.append(f"[image: {Path(link.group(2)).name}]")
            figures.append(link.group(2))
            continue
        if link:
            path = link.group(2)
            if Path(path).suffix.lower() in DOCUMENT_EXTENSIONS:
                document_path = path
                document_label = re.sub(r"^📄\s*", "", link.group(1)).strip()
            else:
                media_path = path
            continue

        warning = _WARNING.match(line)
        if warning:
            warnings.append(warning.group(1).strip())
            continue

        escaped = _ESCAPED.match(line)
        text_lines.append(line[1:] if escaped else line)

    text = "\n".join(text_lines).strip()
    if marker is None:
        free = "\n\n".join(part for part in (heading, text, *notes) if part)
        return None, None, free

    message = ExportedMessage(
        wa_id=str(marker.get("id") or ""),
        chat_id=str(marker.get("chat") or ""),
        author_name=str(marker.get("author") or ("me" if marker.get("from_me") else "unknown")),
        timestamp=int(marker.get("ts") or 0),
        timestamp_inferred=bool(marker.get("inferred")),
        from_me=bool(marker.get("from_me")),
        text=text,
        note="\n\n".join(notes),
        media_path=media_path,
        document_path=document_path,
        document_label=document_label,
        document_hint=marker.get("doc") if isinstance(marker.get("doc"), dict) else {},
        document_text="\n\n".join(part for part in document_parts if part),
        document_figures=figures,
        media_error=" ".join(warnings),
    )
    if not message.wa_id:
        raise MarkdownExportError(f"message marker under '{heading}' has no id")
    return marker, message, ""


def parse_markdown(text: str) -> MarkdownExport:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    front, body = _parse_front_matter(text)
    # HTML comments (the editing guide, or the user's own) are not content --
    # except the per-message markers.
    body = _COMMENT_BLOCK.sub("", body)

    export = MarkdownExport(front_matter=front)
    pending_notes: list[str] = []
    for lines in _split_sections(body):
        _, message, free = _parse_section(lines)
        if message is None:
            if not free:
                continue
            if export.messages:
                last = export.messages[-1]
                last.note = "\n\n".join(part for part in (last.note, free) if part)
            else:
                pending_notes.append(free)
            continue
        if pending_notes:
            message.note = "\n\n".join([*pending_notes, message.note] if message.note else pending_notes)
            pending_notes = []
        export.messages.append(message)
    return export


def guess_mimetype(path: Path) -> str:
    guessed = mimetypes.guess_type(path.name)[0]
    if guessed:
        return guessed
    return {".ogg": "audio/ogg", ".opus": "audio/ogg", ".webp": "image/webp"}.get(
        path.suffix.lower(), "application/octet-stream"
    )


def find_conversation(root: Path) -> Path | None:
    """The export's Markdown file inside a folder, if it has one."""
    candidate = root / CONVERSATION_FILE
    if candidate.is_file():
        return candidate
    for path in sorted(root.glob("*.md")):
        if looks_like_export(path):
            return path
    return None


# -- filling in documents the extension could not include --------------------

_UNSAFE_EXPORT_NAME = re.compile(r'[\\/:*?"<>|#%()\[\]\x00-\x1f]+')


def _export_safe_name(value: str, fallback: str) -> str:
    """Same as exportSafeName in export-format.js."""
    cleaned = _UNSAFE_EXPORT_NAME.sub("_", unicodedata.normalize("NFC", value))
    cleaned = re.sub(r"\s+", "_", cleaned).lstrip(".")[:120]
    return cleaned or fallback


def _short_id(wa_id: str) -> str:
    return _export_safe_name(wa_id.split("_")[-1], "msg")


def _fence_for(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


@dataclass
class FillReport:
    filled: list[str] = field(default_factory=list)  # names now included
    not_found: list[str] = field(default_factory=list)
    no_text: list[str] = field(default_factory=list)  # found, linked, but no readable text


def fill_missing_documents(
    md_path: Path,
    directories: tuple[Path, ...] | list[Path],
    *,
    exclude: tuple[Path, ...] = (),
) -> FillReport:
    """Find documents the export lacks on disk, and write them into it.

    For each message whose marker carries a document hint but whose section
    has no document link, look for the file (document_text.find_document);
    when found, copy it into the export's media/ and replace the section's
    ⚠️ line with the link and the document's text -- exactly what the
    extension would have written had it had the file. The edited file is
    rewritten in place; nothing else in it changes.
    """
    from . import document_text

    report = FillReport()
    lines = md_path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    section: list[str] = []
    in_fence = ""

    def flush() -> None:
        out.extend(_fill_section(section, md_path.parent, directories, exclude, report, document_text))
        section.clear()

    for line in lines:
        if in_fence:
            if line.strip() == in_fence:
                in_fence = ""
        else:
            opened = _FENCE_OPEN.match(line)
            if opened:
                in_fence = opened.group(1)
            elif line.startswith("## "):
                flush()
        section.append(line)
    flush()

    if report.filled or report.no_text:
        md_path.write_text("\n".join(out), encoding="utf-8")
    return report


def _fill_section(section, root, directories, exclude, report, document_text) -> list[str]:
    marker = None
    for line in section:
        found = _MARKER.match(line)
        if found:
            try:
                marker = json.loads(found.group(1))
            except json.JSONDecodeError:
                return section
            break
    hint = marker.get("doc") if isinstance(marker, dict) else None
    if not isinstance(hint, dict) or not hint.get("name"):
        return section
    for line in section:
        link = _MEDIA_LINK.match(line)
        if link and Path(link.group(2)).suffix.lower() in DOCUMENT_EXTENSIONS:
            return section  # already has its document

    name = str(hint["name"])
    source = document_text.find_document(hint, directories, exclude=exclude)
    if source is None:
        report.not_found.append(name)
        return section

    relative = f"media/{_short_id(str(marker.get('id') or ''))}-{_export_safe_name(source.name, 'document')}"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copy2(source, target)

    insert = [f"[📄 {source.name.replace('[', '').replace(']', '')}]({relative})", ""]
    content = document_text.extract_content(source)
    stem = relative[: -len(Path(relative).suffix) or None]
    figure_count = 0
    for kind, value in content.segments():
        if kind == "text":
            fence = _fence_for(value)
            insert += [f"{fence}document", value, fence, ""]
        else:
            figure_count += 1
            figure_path = f"{stem}-fig{figure_count}{value.extension}"
            (root / figure_path).write_bytes(value.data)
            insert += [f"![📄 {value.label}]({figure_path})", ""]
    if content.text.strip():
        report.filled.append(source.name)
    elif content.error:
        insert += [f"⚠️ could not read the text of \"{source.name}\": {content.error}", ""]
        report.no_text.append(source.name)
    else:
        report.no_text.append(source.name)

    # Replace the "document … was attached but not downloaded" warning line,
    # or, if it was edited away, add at the end of the section.
    result = list(section)
    for index, line in enumerate(result):
        if _WARNING.match(line) and "document" in line and name[:20] in line:
            result[index : index + 1] = insert[:-1]
            return result
    while result and not result[-1].strip():
        result.pop()
    return result + ["", *insert]
