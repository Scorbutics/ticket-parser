"""Find a document on disk and read its text.

The browser extension can only include a document's contents if it was
downloaded in WhatsApp *in that tab, since the page was loaded*: a reload
throws WhatsApp's decrypted copy away, and a browser extension is not allowed
to read files from disk. But the file usually *is* on disk, in Downloads,
from whenever you downloaded it. This module finds it there and reads it the
same way extension/export-format.js does, so the result is identical either
way:

- .docx: paragraphs, list items ("- "), table rows ("| a | b |");
- .pdf: the text layer (pypdf), no OCR -- a scan simply has no text;
- .txt/.md/.csv/.log/.json/.xml: as-is.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import zipfile
from pathlib import Path
from xml.etree import ElementTree

log = logging.getLogger(__name__)

MAX_TEXT = 200_000
MAX_SEARCH_DEPTH = 2
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".log", ".json", ".xml"}

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SKIP = {f"{_W}pPr", f"{_W}rPr", f"{_W}instrText", f"{_W}delText"}
# "rapport (1).pdf" -> "rapport.pdf": what browsers do to a second download.
_DUPLICATE_SUFFIX = re.compile(r"\s*\(\d+\)(?=\.[^.]+$|$)")


# -- reading -----------------------------------------------------------------


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []

    def walk(node: ElementTree.Element) -> None:
        for child in node:
            if child.tag in _SKIP:
                continue
            if child.tag == f"{_W}t":
                parts.append(child.text or "")
            elif child.tag == f"{_W}tab":
                parts.append("\t")
            elif child.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
            else:
                walk(child)

    walk(paragraph)
    text = "".join(parts)
    is_list_item = paragraph.find(f".//{_W}numPr") is not None
    return f"- {text}" if is_list_item and text.strip() else text


def _block_text(container: ElementTree.Element) -> str:
    blocks: list[str] = []
    for child in container:
        if child.tag == f"{_W}p":
            blocks.append(_paragraph_text(child))
        elif child.tag == f"{_W}tbl":
            for row in child.iter(f"{_W}tr"):
                cells = [
                    re.sub(r"\n+", " ", _block_text(cell)).strip()
                    for cell in row
                    if cell.tag == f"{_W}tc"
                ]
                blocks.append(f"| {' | '.join(cells)} |")
        elif child.tag == f"{_W}sdt":
            content = child.find(f"{_W}sdtContent")
            if content is not None:
                blocks.append(_block_text(content))
    return "\n".join(blocks)


def docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    body = root.find(f"{_W}body")
    if body is None:
        raise ValueError("no document body")
    return _block_text(body)


def pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError("reading PDFs needs pypdf: pip install pypdf") from exc
    reader = PdfReader(str(path))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    return "\n\n".join(page for page in pages if page)


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "\n[… truncated]"
    return text


def extract_text(path: Path) -> tuple[str, str]:
    """(text, error). Text is '' for a type we do not read or a scanned PDF."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            return _tidy(docx_text(path)), ""
        if suffix == ".pdf":
            return _tidy(pdf_text(path)), ""
        if suffix in TEXT_SUFFIXES:
            return _tidy(path.read_text(encoding="utf-8", errors="replace")), ""
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError, RuntimeError) as exc:
        return "", str(exc)
    except Exception as exc:  # pypdf raises its own error types on broken files
        return "", f"{type(exc).__name__}: {exc}"
    return "", ""


# -- finding -----------------------------------------------------------------


def normalize_name(name: str) -> str:
    name = unicodedata.normalize("NFC", name).lower()
    name = _DUPLICATE_SUFFIX.sub("", name.strip())
    return re.sub(r"\s+", " ", name).strip()


def _candidates(directory: Path, exclude: tuple[Path, ...], depth: int = 0):
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if any(entry == skip for skip in exclude):
            continue
        if entry.is_file():
            yield entry
        elif entry.is_dir() and depth < MAX_SEARCH_DEPTH:
            yield from _candidates(entry, exclude, depth + 1)


def find_document(
    hint: dict, directories: tuple[Path, ...] | list[Path], *, exclude: tuple[Path, ...] = ()
) -> Path | None:
    """The file on disk the export's document hint describes, if any.

    `hint` is what the extension wrote: {"name", "truncated", "type",
    "size": {"min", "max"}}. Matched on the name (a prefix, when WhatsApp
    truncated it), then the type and displayed size break ties; among equals
    the most recently modified wins.
    """
    wanted = normalize_name(str(hint.get("name") or "").rstrip("…").rstrip("."))
    if not wanted:
        return None
    truncated = bool(hint.get("truncated"))
    wanted_type = str(hint.get("type") or "").lower()
    size = hint.get("size") or {}

    matches: list[tuple[int, float, Path]] = []
    for directory in directories:
        directory = Path(directory).expanduser()
        if not directory.is_dir():
            continue
        for path in _candidates(directory, exclude):
            got = normalize_name(path.name)
            if not (got.startswith(wanted) if truncated else got == wanted):
                continue
            if wanted_type and path.suffix.lower().lstrip(".") != wanted_type:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            fits = 1
            if size.get("max"):
                fits = int(size.get("min", 0) <= stat.st_size <= size["max"])
            matches.append((fits, stat.st_mtime, path))

    if not matches:
        return None
    matches.sort(key=lambda m: (m[0], m[1]), reverse=True)
    return matches[0][2]
