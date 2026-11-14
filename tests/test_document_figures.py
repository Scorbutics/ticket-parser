"""Pictures: document figures and screenshots reach the export, the PDF and the model."""

from __future__ import annotations

import json
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from ticket_parser import collect, db, document_text, enrich, investigate, render
from ticket_parser.claude_cli import FakeClaudeRunner
from ticket_parser.config import Config
from ticket_parser.markdown_export import fill_missing_documents, parse_markdown
from ticket_parser.storage import Storage


def png(width: int = 40, height: int = 30, rgb: tuple[int, int, int] = (220, 30, 30)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)


def drawing(rel_id: str) -> str:
    return f'<w:p><w:r><w:drawing><a:graphic><a:blip r:embed="{rel_id}"/></a:graphic></w:drawing></w:r></w:p>'


def make_docx(path: Path) -> Path:
    body = (
        "<w:p><w:r><w:t>Le bouton Valider est rouge :</w:t></w:r></w:p>"
        + drawing("rId7")
        + "<w:p><w:r><w:t>Il devrait être bleu.</w:t></w:r></w:p>"
        + drawing("rId8")  # an EMF: not showable, left out
    )
    rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId7" Type="image" Target="media/image1.png"/>'
        '<Relationship Id="rId8" Type="image" Target="media/image2.emf"/>'
        "</Relationships>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", f"<w:document {NS}><w:body>{body}</w:body></w:document>")
        archive.writestr("word/_rels/document.xml.rels", rels)
        archive.writestr("word/media/image1.png", png())
        archive.writestr("word/media/image2.emf", b"EMF")
    return path


@pytest.fixture
def conn(config: Config):
    return db.connect(config.db_path)


# What extension/export-format.js writes for a .docx with a picture, next to
# a WhatsApp screenshot in another message.
EXPORT = """---
ticket-parser: 1
status: ready
---

## 15/01/2026 09:12 · Emmanuel DE
<!-- msg {"id":"false_336@c.us_A","author":"Emmanuel DE","ts":1768464720} -->

regarde

![screenshot](media/A.png)

## 15/01/2026 09:13 · Emmanuel DE
<!-- msg {"id":"false_336@c.us_B","author":"Emmanuel DE","ts":1768464780} -->

[📄 spec.docx](media/B-spec.docx)

```document
Le bouton Valider est rouge :
```

![📄 figure 1](media/B-spec-fig1.png)

```document
Il devrait être bleu.
```
"""


def write_export(root: Path) -> Path:
    (root / "media").mkdir(parents=True)
    (root / "media" / "A.png").write_bytes(png(rgb=(0, 200, 0)))
    (root / "media" / "B-spec-fig1.png").write_bytes(png())
    make_docx(root / "media" / "B-spec.docx")
    md = root / "conversation.md"
    md.write_text(EXPORT, encoding="utf-8")
    return md


def test_docx_figures_are_extracted_in_place(tmp_path: Path) -> None:
    content = document_text.extract_content(make_docx(tmp_path / "spec.docx"))

    kinds = [kind for kind, _ in content.segments()]
    assert kinds == ["text", "figure", "text"]  # the EMF is left out
    assert content.figures[0].data == png()
    assert content.figures[0].extension == ".png"
    # The text-only view has no trace of the figure.
    assert document_text.extract_text(tmp_path / "spec.docx")[0] == (
        "Le bouton Valider est rouge :\n\nIl devrait être bleu."
    )


def test_figures_are_told_apart_from_screenshots() -> None:
    a, b = parse_markdown(EXPORT).messages

    assert a.media_path == "media/A.png"
    assert a.document_figures == []
    assert b.media_path == ""  # a figure is not the message's screenshot
    assert b.document_figures == ["media/B-spec-fig1.png"]
    assert b.document_text == "Le bouton Valider est rouge :\n\n[image: B-spec-fig1.png]\n\nIl devrait être bleu."


def test_fill_writes_figures_like_the_extension(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    make_docx(downloads / "spec.docx")
    root = tmp_path / "export"
    root.mkdir()
    md = root / "conversation.md"
    md.write_text(
        "---\nticket-parser: 1\n---\n\n## 15/01/2026 09:13 · E\n"
        '<!-- msg {"id":"false_336@c.us_B","ts":1768464780,"doc":{"name":"spec.docx","type":"docx"}} -->\n',
        encoding="utf-8",
    )

    fill_missing_documents(md, [downloads])

    message = parse_markdown(md.read_text(encoding="utf-8")).messages[0]
    assert message.document_text.split("\n\n")[1].startswith("[image: B-spec-fig1.png")
    assert (root / message.document_figures[0]).read_bytes() == png()


def test_figures_reach_the_report_and_the_model(storage: Storage, conn, tmp_path: Path) -> None:
    md = write_export(tmp_path / "drop" / "x")
    collect.import_path(storage, md.parent)

    batch = collect.collect_batch(storage, conn, quiet_period_seconds=0, force=True)
    assert batch is not None
    part = next(m for m in batch.messages if m.is_document_part)
    assert "[image: B-spec-fig1.png]" in part.text
    assert part.image_path("B-spec-fig1.png") is not None  # resolved from the carrier's directory

    enrich.cluster(batch, None, enabled=False)
    html = render.render_html(batch)
    assert html.count('class="figure"') == 1
    assert "figure missing" not in html

    runner = FakeClaudeRunner(
        replies=['{"verdict":"inconclusive","confidence":"low","observations":"x","suggested_fix":"","evidence":[]}']
    )
    investigate.investigate_issue(batch.issues[0], runner, tmp_path, "abc")
    call = runner.calls[0]
    screenshot = (storage.inbox / "false_336_c.us_A" / "false_336_c.us_A.png").resolve()
    figure = (storage.inbox / "false_336_c.us_B" / "B-spec-fig1.png").resolve()
    # Absolute paths in the prompt, and read access to exactly those folders.
    assert f"[screenshot: {screenshot}]" in call["prompt"]
    assert f"[image: {figure}]" in call["prompt"]
    assert "Read tool" in call["prompt"]
    assert set(call["extra_dirs"]) == {screenshot.parent, figure.parent}


def test_a_figure_deleted_from_the_markdown_is_gone(storage: Storage, tmp_path: Path) -> None:
    md = write_export(tmp_path / "drop" / "x")
    md.write_text(md.read_text(encoding="utf-8").replace("![📄 figure 1](media/B-spec-fig1.png)\n", ""), encoding="utf-8")

    collect.import_path(storage, md.parent)

    record = json.loads((storage.inbox / "false_336_c.us_B" / "message.json").read_text())
    assert "[image:" not in record["document_text"]
    assert record["document_images"] == []
