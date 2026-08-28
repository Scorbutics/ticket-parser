from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ticket_parser import collect, db
from ticket_parser.config import Config
from ticket_parser.storage import Storage


@pytest.fixture
def conn(config: Config):
    return db.connect(config.db_path)


def write_message_record(
    storage: Storage,
    *,
    wa_id: str,
    text: str,
    timestamp: int,
    author: str = "Product Owner",
    media: tuple[str, bytes, str] | None = None,
) -> Path:
    """Mimic exactly the record format that lands in inbox/."""
    directory = storage.inbox / wa_id
    directory.mkdir(parents=True)
    record = {
        "schema": 1,
        "id": wa_id,
        "chat_id": "33612345678@c.us",
        "author_name": author,
        "timestamp": timestamp,
        "type": "chat",
        "text": text,
        "media": None,
    }
    if media:
        filename, payload, mimetype = media
        (directory / filename).write_bytes(payload)
        record["media"] = {"filename": filename, "mimetype": mimetype}
    (directory / "message.json").write_text(json.dumps(record), encoding="utf-8")
    return directory


def test_reads_structured_messages_with_media(storage: Storage) -> None:
    write_message_record(
        storage,
        wa_id="msg1",
        text="login is broken 😤",
        timestamp=1_700_000_000,
        media=("IMG-1.jpg", b"\xff\xd8jpeg", "image/jpeg"),
    )

    [message] = [collect.read_message_dir(d, 1) for d in collect.pending_dirs(storage)]

    assert message.text == "login is broken 😤"
    assert message.author_name == "Product Owner"
    assert message.media is not None
    assert message.media.path.read_bytes() == b"\xff\xd8jpeg"


def test_partial_dirs_are_ignored(storage: Storage) -> None:
    write_message_record(storage, wa_id="msg1", text="a", timestamp=1)
    partial = storage.inbox / ".msg2.partial"
    partial.mkdir()
    (partial / "message.json").write_text("{}")

    assert [d.name for d in collect.pending_dirs(storage)] == ["msg1"]


def test_batch_stays_open_while_the_chat_is_active(storage: Storage, conn) -> None:
    now = time.time()
    write_message_record(storage, wa_id="msg1", text="a", timestamp=int(now - 60))

    assert collect.collect_batch(storage, conn, quiet_period_seconds=1800, now=now) is None


def test_batch_closes_after_the_quiet_period(storage: Storage, conn) -> None:
    now = time.time()
    write_message_record(storage, wa_id="msg1", text="a", timestamp=int(now - 3600))
    write_message_record(storage, wa_id="msg2", text="b", timestamp=int(now - 3500))

    batch = collect.collect_batch(storage, conn, quiet_period_seconds=1800, now=now)

    assert batch is not None
    assert [m.text for m in batch.messages] == ["a", "b"]
    assert [m.seq for m in batch.messages] == [1, 2]  # dense ids, chronological


def test_force_closes_an_active_batch(storage: Storage, conn) -> None:
    now = time.time()
    write_message_record(storage, wa_id="msg1", text="a", timestamp=int(now - 5))

    batch = collect.collect_batch(
        storage, conn, quiet_period_seconds=1800, force=True, now=now
    )
    assert batch is not None


def test_already_triaged_messages_do_not_come_back(storage: Storage, conn) -> None:
    now = time.time()
    write_message_record(storage, wa_id="msg1", text="a", timestamp=int(now - 3600))

    first = collect.collect_batch(storage, conn, quiet_period_seconds=1800, now=now)
    assert first is not None
    collect.commit_batch(storage, conn, first)

    write_message_record(storage, wa_id="msg1", text="a", timestamp=int(now - 3600))
    second = collect.collect_batch(storage, conn, quiet_period_seconds=1800, now=now)

    assert second is None  # nothing new -> no empty PDF


def test_unreadable_record_is_surfaced_not_dropped(storage: Storage, conn) -> None:
    now = time.time()
    write_message_record(storage, wa_id="msg1", text="a", timestamp=int(now - 3600))
    broken = storage.inbox / "msg2"
    broken.mkdir()
    (broken / "message.json").write_text("{not json", encoding="utf-8")

    batch = collect.collect_batch(storage, conn, quiet_period_seconds=1800, now=now)

    assert batch is not None
    assert len(batch.messages) == 1
    assert any("unreadable" in warning for warning in batch.warnings)


def test_missing_media_file_keeps_the_text(storage: Storage) -> None:
    directory = write_message_record(
        storage,
        wa_id="msg1",
        text="see this",
        timestamp=1,
        media=("IMG-1.jpg", b"x", "image/jpeg"),
    )
    (directory / "IMG-1.jpg").unlink()

    message = collect.read_message_dir(directory, 1)

    assert message.text == "see this"
    assert message.media is None


def test_commit_moves_dirs_into_done(storage: Storage, conn) -> None:
    now = time.time()
    write_message_record(storage, wa_id="msg1", text="a", timestamp=int(now - 3600))
    batch = collect.collect_batch(storage, conn, quiet_period_seconds=1800, now=now)
    assert batch is not None

    target = collect.commit_batch(storage, conn, batch)

    assert (target / "msg1" / "message.json").is_file()
    assert collect.pending_dirs(storage) == []


def test_a_lost_screenshot_is_reported_not_hidden(storage: Storage, conn) -> None:
    """A failed media download must be visible, not look like a text-only message."""
    import time as _time

    now = _time.time()
    directory = write_message_record(
        storage, wa_id="msg1", text="", timestamp=int(now - 3600)
    )
    record = json.loads((directory / "message.json").read_text())
    record["type"] = "image"
    record["media_error"] = "media was still unavailable after retries"
    (directory / "message.json").write_text(json.dumps(record), encoding="utf-8")

    batch = collect.collect_batch(storage, conn, quiet_period_seconds=1800, now=now)

    assert batch is not None
    assert batch.messages[0].media is None
    assert batch.messages[0].media_error
    assert any("could not be retrieved" in w for w in batch.warnings)


def test_the_page_says_where_a_screenshot_was_lost(storage: Storage, conn) -> None:
    from ticket_parser import enrich, render

    import time as _time

    now = _time.time()
    directory = write_message_record(
        storage, wa_id="msg1", text="regarde ça", timestamp=int(now - 3600)
    )
    record = json.loads((directory / "message.json").read_text())
    record["media_error"] = "media could not be downloaded"
    (directory / "message.json").write_text(json.dumps(record), encoding="utf-8")

    batch = collect.collect_batch(storage, conn, quiet_period_seconds=1800, now=now)
    assert batch is not None
    enrich.cluster(batch, None, enabled=False)

    rendered = render.render_html(batch)

    assert "could not be retrieved" in rendered
    assert "regarde ça" in rendered  # the text still survives
