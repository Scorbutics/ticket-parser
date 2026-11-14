"""The editable Markdown export: parsed back with every allowed edit honoured."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from ticket_parser import collect, db, documents, enrich, investigate, render
from ticket_parser.config import Config
from ticket_parser.markdown_export import parse_markdown
from ticket_parser.storage import Storage

# What extension/export-format.js writes, after a person edited it: the second
# message's text was changed and a note added, a free-standing section was
# added, and a third message was deleted entirely.
EXPORT = """---
ticket-parser: 1
status: {status}
chat: 336@c.us
exported_at: 2026-11-15T10:00:00.000Z
messages: 4
---

# WhatsApp — Emmanuel DE — 15/01/2026 → 15/01/2026

<!--
editing guide, ignored
## not a section either
-->

## 15/01/2026 09:12 · Emmanuel DE
<!-- msg {{"id":"false_336@c.us_A","chat":"336@c.us","author":"Emmanuel DE","ts":1768464720,"inferred":false,"from_me":false}} -->

le badge ne s'affiche pas
\\## this line is text, not a heading
\\> Note: neither is this

![screenshot](media/A.jpg)

## ~15/01/2026 09:15 · Emmanuel DE
<!-- msg {{"id":"false_336@c.us_B","chat":"336@c.us","author":"Emmanuel DE","ts":1768464900,"inferred":true,"from_me":false}} -->

voici le doc (edited by me)

> Note: only on Android,
> see ticket 42

[📄 Amélioration PEPS.docx](media/B-Amélioration_PEPS.docx)

````document
Création badge
Le badge doit afficher le nom.
```not a fence end```
````

## My own remark
This one is about the login screen too.

## 15/01/2026 09:20 · Emmanuel DE
<!-- msg {{"id":"false_336@c.us_D","chat":"336@c.us","author":"Emmanuel DE","ts":1768465200,"inferred":true,"from_me":false}} -->

[🎤 Voice note](media/D.ogg)

⚠️ document "x.pdf" was attached but not downloaded
"""


@pytest.fixture
def conn(config: Config):
    return db.connect(config.db_path)


def write_export(root: Path, *, status: str = "draft") -> Path:
    (root / "media").mkdir(parents=True)
    (root / "media" / "A.jpg").write_bytes(b"\xff\xd8jpeg")
    (root / "media" / "B-Amélioration_PEPS.docx").write_bytes(b"PK\x03\x04docx")
    (root / "media" / "D.ogg").write_bytes(b"OggS")
    md = root / "conversation.md"
    md.write_text(EXPORT.format(status=status), encoding="utf-8")
    return md


def test_parse_honours_edits_notes_and_escapes() -> None:
    export = parse_markdown(EXPORT.format(status="draft"))

    assert not export.ready
    assert [m.wa_id for m in export.messages] == [
        "false_336@c.us_A",
        "false_336@c.us_B",
        "false_336@c.us_D",
    ]
    a, b, d = export.messages
    assert a.text == (
        "le badge ne s'affiche pas\n## this line is text, not a heading\n> Note: neither is this"
    )
    assert a.note == ""
    assert a.media_path == "media/A.jpg"

    assert b.text == "voici le doc (edited by me)"
    assert b.timestamp_inferred
    assert b.document_path == "media/B-Amélioration_PEPS.docx"
    assert b.document_text == "Création badge\nLe badge doit afficher le nom.\n```not a fence end```"
    # Its own note, then the free-standing section that followed it.
    assert b.note == (
        "only on Android,\nsee ticket 42\n\nMy own remark\n\nThis one is about the login screen too."
    )

    assert d.media_path == "media/D.ogg"
    assert d.media_error.startswith('document "x.pdf"')


def test_import_folder_copies_media_and_keeps_the_edited_file(
    storage: Storage, tmp_path: Path
) -> None:
    md = write_export(tmp_path / "drop" / "whatsapp-export")

    imported, skipped = collect.import_path(storage, md.parent)

    assert (imported, skipped) == (3, 0)
    record = json.loads((storage.inbox / "false_336_c.us_B" / "message.json").read_text())
    assert record["text"] == "voici le doc (edited by me)"
    assert record["note"].startswith("only on Android")
    assert record["document_name"] == "Amélioration PEPS.docx"
    assert record["document_text"].startswith("Création badge")
    assert (storage.inbox / "false_336_c.us_B" / record["document_file"]).is_file()
    voice = json.loads((storage.inbox / "false_336_c.us_D" / "message.json").read_text())
    assert voice["media"] == {"filename": "false_336_c.us_D.ogg", "mimetype": "audio/ogg"}
    # The user's edited file is kept, renamed so it is not imported twice.
    assert not md.exists()
    assert md.with_name("conversation.md.imported").is_file()


def test_watch_leaves_drafts_alone_until_ready(storage: Storage, tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    md = write_export(drop / "whatsapp-export")

    assert collect.import_drop(storage, drop, include_drafts=False) == (0, 0)
    assert md.exists()

    md.write_text(md.read_text(encoding="utf-8").replace("status: draft", "status: ready"), encoding="utf-8")
    assert collect.import_drop(storage, drop, include_drafts=False) == (3, 0)


def test_media_links_cannot_escape_the_export(storage: Storage, tmp_path: Path) -> None:
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"nope")
    md = write_export(tmp_path / "drop" / "x")
    md.write_text(
        md.read_text(encoding="utf-8").replace("media/A.jpg", "media/../../../secret.jpg"),
        encoding="utf-8",
    )

    collect.import_path(storage, md.parent)

    record = json.loads((storage.inbox / "false_336_c.us_A" / "message.json").read_text())
    assert record["media"] is None
    assert "missing" in record["media_error"]


def test_zip_export_is_imported_and_removed(storage: Storage, tmp_path: Path) -> None:
    source = tmp_path / "src"
    write_export(source, status="draft")
    drop = tmp_path / "drop"
    drop.mkdir()
    archive = drop / "whatsapp-export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in source.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(source).as_posix())

    # A zip is transport: imported even though it says draft.
    assert collect.import_drop(storage, drop, include_drafts=False) == (3, 0)
    assert not archive.exists()


def test_split_document_cuts_at_boundaries_without_losing_text() -> None:
    text = "\n".join(
        ["Compte rendu", ""]
        + [f"{i}. Le bouton {i} ne réagit pas quand on clique dessus depuis la page d'accueil, "
           "ni depuis le menu latéral." for i in range(1, 9)]
    )
    parts = documents.split_document(text)

    assert len(parts) > 1
    assert all(len(p) <= documents.HARD_CHARS for p in parts)
    # Verbatim: putting the parts back together loses nothing but whitespace.
    assert "".join("".join(parts).split()) == "".join(text.split())


def test_document_parts_get_their_own_ids_and_render(
    storage: Storage, conn, tmp_path: Path
) -> None:
    md = write_export(tmp_path / "drop" / "x")
    long_doc = "\n".join(f"- point {i}: " + "le badge ne marche pas " * 8 for i in range(6))
    md.write_text(
        md.read_text(encoding="utf-8").replace(
            "Création badge\nLe badge doit afficher le nom.", long_doc
        ),
        encoding="utf-8",
    )
    collect.import_path(storage, md.parent)

    batch = collect.collect_batch(storage, conn, quiet_period_seconds=0, force=True)

    assert batch is not None
    parts = [m for m in batch.messages if m.is_document_part]
    carrier = next(m for m in batch.messages if m.wa_id == "false_336@c.us_B")
    assert len(parts) == carrier.document_parts > 1
    # Parts follow their message and have their own ids for clustering.
    assert [m.seq for m in parts] == list(range(carrier.seq + 1, carrier.seq + 1 + len(parts)))
    prompt = enrich.build_prompt(batch.messages)
    assert f'[{parts[0].seq}] Emmanuel DE (attached document "Amélioration PEPS.docx", part 1/' in prompt
    assert "reviewer note: only on Android" in prompt

    # A part grouped with a chat message: both land on the same issue page.
    payload = [{"message_ids": [1, parts[1].seq], "title": "Badge", "category": "ui", "severity": "low"}]
    batch.issues = enrich._issues_from_payload(payload, batch)
    issue = batch.issues[0]
    assert [m.seq for m in issue.messages] == [1, parts[1].seq]
    quote = investigate.build_prompt(issue)
    assert "<document>" in quote and "point 1" in quote

    html = render.render_html(batch)
    assert "part 2/" in html
    assert "Note:" in html


def test_an_indented_note_is_still_a_note() -> None:
    # Markdown allows up to three spaces before a blockquote's ">".
    text = EXPORT.format(status="draft").replace("> Note: only on Android,\n> see ticket 42", "  > Note: only on Android,\n  > see ticket 42")

    b = parse_markdown(text).messages[1]

    assert b.text == "voici le doc (edited by me)"
    assert b.note.startswith("only on Android,\nsee ticket 42")
