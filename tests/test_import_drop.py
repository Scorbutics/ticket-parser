"""The Firefox extension writes into a downloads folder; import lifts it in."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ticket_parser import collect, db
from ticket_parser.config import Config
from ticket_parser.storage import Storage


@pytest.fixture
def conn(config: Config):
    return db.connect(config.db_path)


def write_extension_export(
    drop: Path,
    *,
    message_id: str,
    text: str,
    timestamp: int,
    image: bytes | None = None,
    inferred: bool = False,
) -> Path:
    directory = drop / message_id
    directory.mkdir(parents=True)
    record = {
        "schema": 1,
        "id": message_id,
        "chat_id": "33603063403@c.us",
        "author_name": "Emmanuel DE",
        "timestamp": timestamp,
        "timestamp_inferred": inferred,
        "type": "image" if image else "chat",
        "text": text,
        "from_me": False,
        "media": None,
        "source": "firefox-extension",
    }
    if image:
        (directory / f"{message_id}.png").write_bytes(image)
        record["media"] = {"filename": f"{message_id}.png", "mimetype": "image/png"}
    (directory / "message.json").write_text(json.dumps(record), encoding="utf-8")
    return directory


def test_import_moves_messages_and_media_into_inbox(
    storage: Storage, tmp_path: Path
) -> None:
    drop = tmp_path / "drop"
    write_extension_export(drop, message_id="false_x_A", text="login broken", timestamp=1)
    write_extension_export(
        drop, message_id="false_x_B", text="", timestamp=2, image=b"\x89PNG"
    )

    imported, skipped = collect.import_drop(storage, drop)

    assert (imported, skipped) == (2, 0)
    assert (storage.inbox / "false_x_A" / "message.json").is_file()
    assert (storage.inbox / "false_x_B" / "false_x_B.png").read_bytes() == b"\x89PNG"
    assert list(drop.iterdir()) == []  # consumed, so a re-export is not re-imported


def test_import_is_idempotent(storage: Storage, tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    write_extension_export(drop, message_id="false_x_A", text="a", timestamp=1)
    collect.import_drop(storage, drop)

    write_extension_export(drop, message_id="false_x_A", text="a", timestamp=1)
    imported, skipped = collect.import_drop(storage, drop)

    assert (imported, skipped) == (0, 1)


def test_extension_messages_flow_through_the_pipeline(
    storage: Storage, conn, tmp_path: Path
) -> None:
    import time

    from ticket_parser import enrich, render

    now = time.time()
    drop = tmp_path / "drop"
    write_extension_export(
        drop, message_id="false_x_A", text="le login est cassé", timestamp=int(now - 9000)
    )
    write_extension_export(
        drop,
        message_id="false_x_B",
        text="",
        timestamp=int(now - 8900),
        image=b"\x89PNG",
        inferred=True,
    )

    collect.import_drop(storage, drop)
    batch = collect.collect_batch(storage, conn, quiet_period_seconds=7200, now=now)

    assert batch is not None
    assert [m.text for m in batch.messages] == ["le login est cassé", ""]
    assert batch.messages[0].author_name == "Emmanuel DE"
    assert batch.messages[1].media is not None
    assert any("inferred" in w for w in batch.warnings)

    enrich.cluster(batch, None, enabled=False)
    rendered = render.render_html(batch)
    assert "le login est cassé" in rendered


def test_missing_drop_dir_is_a_clear_error(storage: Storage, tmp_path: Path) -> None:
    with pytest.raises(collect.CollectError, match="does not exist"):
        collect.import_drop(storage, tmp_path / "nope")


def test_incomplete_download_is_ignored(storage: Storage, tmp_path: Path) -> None:
    """A directory without message.json is a download still in flight."""
    drop = tmp_path / "drop"
    (drop / "half-written").mkdir(parents=True)
    (drop / "half-written" / "image.png").write_bytes(b"x")

    imported, skipped = collect.import_drop(storage, drop)

    assert (imported, skipped) == (0, 0)
    assert (drop / "half-written").exists()


def test_document_attachments_are_named_on_the_page(
    storage: Storage, conn, tmp_path: Path
) -> None:
    """WhatsApp only decrypts documents on click, so we name them, not drop them."""
    import time

    from ticket_parser import collect as _collect, enrich, render

    now = time.time()
    drop = tmp_path / "drop"
    directory = write_extension_export(
        drop, message_id="false_x_D", text="voici le cahier des charges", timestamp=int(now - 9000)
    )
    record = json.loads((directory / "message.json").read_text())
    record["type"] = "document"
    record["document_name"] = "cahier-des-charges.docx"
    record["media_error"] = 'document "cahier-des-charges.docx" was attached'
    (directory / "message.json").write_text(json.dumps(record), encoding="utf-8")

    _collect.import_drop(storage, drop)
    batch = _collect.collect_batch(storage, conn, quiet_period_seconds=7200, now=now)

    assert batch is not None
    assert batch.messages[0].document_name == "cahier-des-charges.docx"
    assert any("cahier-des-charges.docx" in w for w in batch.warnings)
    # Not reported as a lost screenshot -- it is a different thing.
    assert not any("could not be retrieved" in w for w in batch.warnings)

    enrich.cluster(batch, None, enabled=False)
    rendered = render.render_html(batch)
    assert "document attached" in rendered
    assert "cahier-des-charges.docx" in rendered
