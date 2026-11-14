"""Find a document on disk and read its text.

The browser extension can only include a document's contents if it was
downloaded in WhatsApp *in that tab, since the page was loaded*: a reload
throws WhatsApp's decrypted copy away, and a browser extension is not allowed
to read files from disk. But the file usually *is* on disk, in Downloads,
from whenever you downloaded it. This module finds it there and reads it the
same way extension/export-format.js does, so the result is identical either
way:

- .docx: paragraphs, list items ("- "), table rows ("| a | b |"), and its
  pictures, in place;
- .pdf: the text layer (pypdf), no OCR -- a scan simply has no text -- plus
  the images embedded in each page, after that page's text (the extension
  takes a picture of the whole page instead; pypdf cannot render pages);
- .txt/.md/.csv/.log/.json/.xml: as-is.

A figure's place in the text is a line holding FIGURE_MARK.format(n), n
indexing `DocumentContent.figures`; whoever writes the text out replaces it.
"""

from __future__ import annotations

import logging
import posixpath
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

log = logging.getLogger(__name__)

MAX_TEXT = 200_000
MAX_SEARCH_DEPTH = 2
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".log", ".json", ".xml"}

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_V = "{urn:schemas-microsoft-com:vml}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

FIGURE_MARK = "\x00figure:{}\x00"
_FIGURE_LINE = re.compile(r"^\x00figure:(\d+)\x00$", re.MULTILINE)
# What a browser, the PDF renderer and the model can all show. (EMF/WMF
# vector pictures from Word are none of those, and are left out.)
SHOWABLE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_FIGURES = 40
MIN_FIGURE_BYTES = 1500  # spacer pixels, bullets, tiles of a background


@dataclass
class Figure:
    data: bytes
    extension: str  # ".png"
    label: str  # "figure 1", "image from page 3"


@dataclass
class DocumentContent:
    text: str = ""
    figures: list[Figure] = field(default_factory=list)
    error: str = ""

    def segments(self) -> list[tuple[str, object]]:
        """[("text", str) | ("figure", Figure)] in document order."""
        out: list[tuple[str, object]] = []
        last = 0
        for match in _FIGURE_LINE.finditer(self.text):
            chunk = self.text[last : match.start()].strip("\n")
            if chunk.strip():
                out.append(("text", chunk))
            index = int(match.group(1))
            if index < len(self.figures):
                out.append(("figure", self.figures[index]))
            last = match.end()
        tail = self.text[last:].strip("\n")
        if tail.strip():
            out.append(("text", tail))
        return out
_SKIP = {f"{_W}pPr", f"{_W}rPr", f"{_W}instrText", f"{_W}delText"}
# "rapport (1).pdf" -> "rapport.pdf": what browsers do to a second download.
_DUPLICATE_SUFFIX = re.compile(r"\s*\(\d+\)(?=\.[^.]+$|$)")


# -- reading -----------------------------------------------------------------


class _DocxFigures:
    def __init__(self, archive: zipfile.ZipFile) -> None:
        self.archive = archive
        self.figures: list[Figure] = []
        self.targets: dict[str, str] = {}
        try:
            rels = ElementTree.fromstring(archive.read("word/_rels/document.xml.rels"))
            for rel in rels.iter(f"{_REL}Relationship"):
                if rel.get("TargetMode") != "External":
                    self.targets[rel.get("Id", "")] = posixpath.normpath(
                        posixpath.join("word", rel.get("Target", ""))
                    )
        except KeyError:
            pass

    def mark(self, node: ElementTree.Element) -> str:
        """The figure line for a w:drawing / w:pict, or '' if not showable."""
        rel_id = ""
        blip = node.find(f".//{_A}blip")
        if blip is not None:
            rel_id = blip.get(f"{_R}embed", "")
        else:
            imagedata = node.find(f".//{_V}imagedata")
            if imagedata is not None:
                rel_id = imagedata.get(f"{_R}id", "")
        target = self.targets.get(rel_id)
        if not target or len(self.figures) >= MAX_FIGURES:
            return ""
        extension = posixpath.splitext(target)[1].lower()
        if extension not in SHOWABLE:
            return ""
        try:
            data = self.archive.read(target)
        except KeyError:
            return ""
        self.figures.append(Figure(data, ".jpg" if extension == ".jpeg" else extension, f"figure {len(self.figures) + 1}"))
        return "\n" + FIGURE_MARK.format(len(self.figures) - 1) + "\n"


def _paragraph_text(paragraph: ElementTree.Element, figures: _DocxFigures) -> str:
    parts: list[str] = []

    def walk(node: ElementTree.Element) -> None:
        for child in node:
            if child.tag in _SKIP:
                continue
            if child.tag in (f"{_W}drawing", f"{_W}pict"):
                parts.append(figures.mark(child))
            elif child.tag == f"{_W}t":
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


def _block_text(container: ElementTree.Element, figures: _DocxFigures) -> str:
    blocks: list[str] = []
    for child in container:
        if child.tag == f"{_W}p":
            blocks.append(_paragraph_text(child, figures))
        elif child.tag == f"{_W}tbl":
            for row in child.iter(f"{_W}tr"):
                cells = [
                    re.sub(r"\n+", " ", _block_text(cell, figures)).strip()
                    for cell in row
                    if cell.tag == f"{_W}tc"
                ]
                blocks.append(f"| {' | '.join(cells)} |")
        elif child.tag == f"{_W}sdt":
            content = child.find(f"{_W}sdtContent")
            if content is not None:
                blocks.append(_block_text(content, figures))
    return "\n".join(blocks)


def docx_content(path: Path) -> DocumentContent:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
        body = root.find(f"{_W}body")
        if body is None:
            raise ValueError("no document body")
        figures = _DocxFigures(archive)
        text = _block_text(body, figures)
    return DocumentContent(text=text, figures=figures.figures)


def docx_text(path: Path) -> str:
    return docx_content(path).text


def pdf_content(path: Path) -> DocumentContent:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError("reading PDFs needs pypdf: pip install pypdf") from exc
    reader = PdfReader(str(path))
    pages: list[str] = []
    figures: list[Figure] = []
    for number, page in enumerate(reader.pages, start=1):
        chunk = (page.extract_text() or "").strip()
        try:
            images = list(page.images)
        except Exception as exc:  # an image pypdf cannot decode must not cost the text
            log.warning("could not read images of a PDF page", extra={"page": number, "error": str(exc)})
            images = []
        for image in images:
            extension = Path(image.name).suffix.lower()
            if extension not in SHOWABLE or len(image.data) < MIN_FIGURE_BYTES or len(figures) >= MAX_FIGURES:
                continue
            figures.append(Figure(image.data, ".jpg" if extension == ".jpeg" else extension, f"image from page {number}"))
            chunk += "\n" + FIGURE_MARK.format(len(figures) - 1)
        if chunk.strip():
            pages.append(chunk.strip())
    return DocumentContent(text="\n\n".join(pages), figures=figures)


def pdf_text(path: Path) -> str:
    return pdf_content(path).text


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # A figure always on a line of its own.
    text = re.sub(r"[ \t]*(\x00figure:\d+\x00)[ \t]*", r"\n\1\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "\n[… truncated]"
    return text


def extract_content(path: Path) -> DocumentContent:
    """Text and figures. Empty for a type we do not read, or a scanned PDF."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            content = docx_content(path)
        elif suffix == ".pdf":
            content = pdf_content(path)
        elif suffix in TEXT_SUFFIXES:
            content = DocumentContent(text=path.read_text(encoding="utf-8", errors="replace"))
        else:
            return DocumentContent()
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError, RuntimeError) as exc:
        return DocumentContent(error=str(exc))
    except Exception as exc:  # pypdf raises its own error types on broken files
        return DocumentContent(error=f"{type(exc).__name__}: {exc}")
    content.text = _tidy(content.text)
    return content


def extract_text(path: Path) -> tuple[str, str]:
    """(text, error), figures dropped. Text is '' for a type we do not read."""
    content = extract_content(path)
    return re.sub(r"\n{3,}", "\n\n", _FIGURE_LINE.sub("", content.text)).strip(), content.error


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
