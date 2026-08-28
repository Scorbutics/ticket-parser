"""The gzipped bundle: what the phone shares and what email carries."""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path

import pytest

from ticket_parser import collect, db
from ticket_parser.config import Config
from ticket_parser.storage import Storage

PNG = b"\x89PNG\r\n\x1a\n-fake"


@pytest.fixture
def conn(config: Config):
    return db.connect(config.db_path)


def make_bundle(path: Path, *, messages: list[dict]) -> Path:
    payload = {
        "schema": 1,
        "source": "firefox-extension",
        "exported_at": 1_700_000_000,
        "message_count": len(messages),
        "messages": messages,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


def message(
    message_id: str, text: str, timestamp: int, *, image: bytes | None = None
) -> dict:
    record = {
        "schema": 1,
        "id": message_id,
        "chat_id": "33603063403@c.us",
        "author_name": "Emmanuel DE",
        "timestamp": timestamp,
        "type": "image" if image else "chat",
        "text": text,
        "from_me": False,
        "media": None,
    }
    if image:
        record["media"] = {
            "base64": base64.b64encode(image).decode(),
            "mimetype": "image/png",
            "extension": ".png",
        }
    return record


def test_bundle_unpacks_into_the_normal_layout(
    storage: Storage, tmp_path: Path
) -> None:
    bundle = make_bundle(
        tmp_path / "drop" / "whatsapp-20260813T1900.json.gz",
        messages=[
            message("false_x_A", "le login est cassé", 1),
            message("false_x_B", "", 2, image=PNG),
        ],
    )

    imported, skipped = collect.import_bundle(storage, bundle)

    assert (imported, skipped) == (2, 0)
    record = json.loads((storage.inbox / "false_x_A" / "message.json").read_text())
    assert record["text"] == "le login est cassé"
    # Media is written as a real file, and the base64 blob is not kept around.
    assert (storage.inbox / "false_x_B" / "false_x_B.png").read_bytes() == PNG
    stored = json.loads((storage.inbox / "false_x_B" / "message.json").read_text())
    assert stored["media"] == {"filename": "false_x_B.png", "mimetype": "image/png"}
    assert "base64" not in json.dumps(stored)


def test_bundle_in_the_drop_folder_is_imported_and_consumed(
    storage: Storage, tmp_path: Path
) -> None:
    drop = tmp_path / "drop"
    make_bundle(drop / "export.json.gz", messages=[message("false_x_A", "a", 1)])

    imported, _ = collect.import_drop(storage, drop)

    assert imported == 1
    assert list(drop.iterdir()) == []  # consumed, so it is not re-imported


def test_reimporting_the_same_bundle_changes_nothing(
    storage: Storage, tmp_path: Path
) -> None:
    messages = [message("false_x_A", "a", 1)]
    make_bundle(tmp_path / "drop" / "export.json.gz", messages=messages)
    collect.import_drop(storage, tmp_path / "drop")

    make_bundle(tmp_path / "drop" / "export.json.gz", messages=messages)
    imported, skipped = collect.import_drop(storage, tmp_path / "drop")

    assert (imported, skipped) == (0, 1)


def test_uncompressed_json_is_accepted_too(storage: Storage, tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    drop.mkdir(parents=True)
    (drop / "export.json").write_text(
        json.dumps({"messages": [message("false_x_A", "a", 1)]}), encoding="utf-8"
    )

    imported, _ = collect.import_drop(storage, drop)

    assert imported == 1


def test_a_corrupt_bundle_is_reported_not_swallowed(
    storage: Storage, tmp_path: Path
) -> None:
    bundle = tmp_path / "drop" / "broken.json.gz"
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(b"this is not gzip")

    with pytest.raises(collect.CollectError, match="unreadable bundle"):
        collect.import_bundle(storage, bundle)


def test_a_corrupt_bundle_does_not_block_a_good_one(
    storage: Storage, tmp_path: Path
) -> None:
    drop = tmp_path / "drop"
    drop.mkdir(parents=True)
    (drop / "a-broken.json.gz").write_bytes(b"not gzip")
    make_bundle(drop / "b-good.json.gz", messages=[message("false_x_A", "a", 1)])

    imported, _ = collect.import_drop(storage, drop)

    assert imported == 1
    assert (drop / "a-broken.json.gz").exists()  # left for inspection


def test_undecodable_media_keeps_the_message(storage: Storage, tmp_path: Path) -> None:
    record = message("false_x_A", "regarde", 1)
    record["media"] = {"base64": "!!!not base64!!!", "mimetype": "image/png"}
    bundle = make_bundle(tmp_path / "drop" / "e.json.gz", messages=[record])

    imported, _ = collect.import_bundle(storage, bundle)

    assert imported == 1
    stored = json.loads((storage.inbox / "false_x_A" / "message.json").read_text())
    assert stored["text"] == "regarde"
    assert "could not be decoded" in stored["media_error"]


def test_bundle_flows_through_to_a_rendered_report(
    storage: Storage, conn, tmp_path: Path
) -> None:
    import time

    from ticket_parser import enrich, render

    now = time.time()
    make_bundle(
        tmp_path / "drop" / "export.json.gz",
        messages=[
            message("false_x_A", "le panier est faux", int(now - 9000)),
            message("false_x_B", "", int(now - 8900), image=PNG),
        ],
    )

    collect.import_drop(storage, tmp_path / "drop")
    batch = collect.collect_batch(storage, conn, quiet_period_seconds=7200, now=now)

    assert batch is not None
    assert [m.text for m in batch.messages] == ["le panier est faux", ""]
    assert batch.messages[1].media is not None

    enrich.cluster(batch, None, enabled=False)
    assert "le panier est faux" in render.render_html(batch)
