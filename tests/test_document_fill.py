"""Documents the extension could not include are found on disk and filled in."""

from __future__ import annotations

import json
import os
import time
import zipfile
from pathlib import Path

import pytest

from ticket_parser import collect, document_text
from ticket_parser.markdown_export import fill_missing_documents, parse_markdown
from ticket_parser.storage import Storage

DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:r><w:t>Amélioration PEPS – création badge</w:t></w:r></w:p>
<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t xml:space="preserve">Le badge </w:t></w:r><w:r><w:t>doit afficher le nom.</w:t></w:r></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Écran</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Ma base</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
</w:body></w:document>"""

# What extension/export-format.js extracted from this same document.
EXTENSION_TEXT = "Amélioration PEPS – création badge\n- Le badge doit afficher le nom.\n| Écran | Ma base |"

FULL_NAME = "V2-Amélioration PEPS – Ma Base-Mes équipes – 111126.docx"


def make_docx(path: Path, pad_to: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", DOCUMENT_XML)
        if pad_to:
            archive.writestr("pad.bin", os.urandom(pad_to))
    return path


def export_with_missing_document(root: Path, hint: dict) -> Path:
    marker = {
        "id": "false_336@c.us_3EB0AB",
        "chat": "336@c.us",
        "author": "Emmanuel DE",
        "ts": 1768464720,
        "inferred": True,
        "from_me": False,
        "doc": hint,
    }
    root.mkdir(parents=True, exist_ok=True)
    md = root / "conversation.md"
    md.write_text(
        "---\nticket-parser: 1\nstatus: draft\n---\n\n"
        "## 15/01/2026 09:12 · Emmanuel DE\n"
        f"<!-- msg {json.dumps(marker, ensure_ascii=False)} -->\n\n"
        "voici le nouvel agencement\n\n"
        f'⚠️ document "{hint["name"]}" was attached but not downloaded in WhatsApp before exporting\n\n'
        "## 15/01/2026 09:13 · Emmanuel DE\n"
        '<!-- msg {"id":"false_336@c.us_B","ts":1768464780,"author":"Emmanuel DE"} -->\n\n'
        "next message\n",
        encoding="utf-8",
    )
    return md


def test_docx_text_matches_what_the_extension_extracts(tmp_path: Path) -> None:
    text, error = document_text.extract_text(make_docx(tmp_path / "a.docx"))
    assert (text, error) == (EXTENSION_TEXT, "")


def test_truncated_name_is_found_and_filled_in_place(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    # A browser's second copy of the same download: "name (1).docx".
    make_docx(downloads / FULL_NAME.replace(".docx", " (1).docx"))
    make_docx(downloads / "unrelated.docx")
    md = export_with_missing_document(
        tmp_path / "export",
        {"name": "V2-Amélioration PEPS – Ma Base-Mes équipes – 111126", "truncated": True, "type": "docx", "size": None},
    )

    report = fill_missing_documents(md, [downloads])

    assert report.filled == [FULL_NAME.replace(".docx", " (1).docx")]
    message = parse_markdown(md.read_text(encoding="utf-8")).messages[0]
    assert message.text == "voici le nouvel agencement"
    assert message.document_text == EXTENSION_TEXT
    assert message.media_error == ""  # the "not downloaded" warning is gone
    assert (md.parent / message.document_path).is_file()
    # The rest of the file is untouched.
    assert parse_markdown(md.read_text(encoding="utf-8")).messages[1].text == "next message"
    # Running it again changes nothing.
    assert fill_missing_documents(md, [downloads]).filled == []


def test_size_breaks_ties_and_the_drop_folder_is_skipped(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    small = make_docx(downloads / "old" / FULL_NAME)
    big = make_docx(downloads / FULL_NAME, pad_to=60_000)
    # Newer, same name, but inside the drop folder: must be ignored.
    make_docx(downloads / "ticket-parser" / FULL_NAME)
    os.utime(big, (time.time() - 3600, time.time() - 3600))  # older than `small`

    size = big.stat().st_size
    hint = {"name": FULL_NAME, "truncated": False, "type": "docx", "size": {"min": size - 500, "max": size + 500}}

    found = document_text.find_document(hint, [downloads], exclude=(downloads / "ticket-parser",))

    assert found == big  # the size fits, even though `small` is newer
    assert small.exists()


def test_not_found_is_reported_and_the_file_left_alone(tmp_path: Path) -> None:
    md = export_with_missing_document(tmp_path / "export", {"name": "absent.pdf", "truncated": False, "type": "pdf"})
    before = md.read_text(encoding="utf-8")

    report = fill_missing_documents(md, [tmp_path / "Downloads"])

    assert report.not_found == ["absent.pdf"]
    assert md.read_text(encoding="utf-8") == before
    # Still imported under its name, with the warning, rather than lost.
    message = parse_markdown(before).messages[0]
    assert message.record()["document_name"] == "absent.pdf"
    assert "not downloaded" in message.media_error


def test_import_fills_documents_before_importing(storage: Storage, tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    make_docx(downloads / FULL_NAME)
    md = export_with_missing_document(
        tmp_path / "drop" / "export", {"name": FULL_NAME, "truncated": False, "type": "docx"}
    )

    collect.import_path(storage, md.parent, document_dirs=(downloads,))

    record = json.loads((storage.inbox / "false_336_c.us_3EB0AB" / "message.json").read_text())
    assert record["document_text"] == EXTENSION_TEXT
    assert record["document_name"] == FULL_NAME
    assert (storage.inbox / "false_336_c.us_3EB0AB" / record["document_file"]).is_file()


def test_pdf_text_layer_is_read(tmp_path: Path) -> None:
    pytest.importorskip("pypdf")
    stream = b"BT /F1 12 Tf 20 150 Td (Le menu Mes equipes plante) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path = tmp_path / "doc.pdf"
    path.write_bytes(out)

    text, error = document_text.extract_text(path)

    assert error == ""
    assert "Le menu Mes equipes plante" in text
