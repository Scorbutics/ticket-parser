from __future__ import annotations

import email
import email.policy
from email.message import EmailMessage
from pathlib import Path

import pytest

from ticket_parser import db, ingest
from ticket_parser.config import Config
from ticket_parser.storage import Storage, slug_for


def build_email(
    *,
    message_id: str = "<msg-1@gmx.com>",
    sender: str = "me@example.invalid",
    attachments: tuple[tuple[str, bytes], ...] = (),
) -> bytes:
    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["From"] = sender
    msg["To"] = "me@example.invalid"
    msg["Subject"] = "Bugs"
    msg["Date"] = "Thu, 13 Aug 2026 14:02:00 +0200"
    msg.set_content("15/01/2024, 09:12 - Owner: login is broken")
    for name, payload in attachments:
        msg.add_attachment(
            payload, maintype="image", subtype="jpeg", filename=name
        )
    return msg.as_bytes()


class FakeImap:
    """Stands in for ImapClient; records deletes so ordering can be asserted."""

    def __init__(self, messages: dict[bytes, bytes]) -> None:
        self.messages = messages
        self.deleted: list[bytes] = []
        self.expunged = False

    def search_uids(self) -> list[bytes]:
        return [uid for uid in self.messages if uid not in self.deleted]

    def fetch(self, uid: bytes) -> bytes:
        return self.messages[uid]

    def delete(self, uid: bytes) -> None:
        self.deleted.append(uid)

    def expunge(self) -> None:
        self.expunged = True


@pytest.fixture
def conn(config: Config):
    return db.connect(config.db_path)


def test_ingest_writes_eml_and_attachments(config: Config, storage: Storage, conn) -> None:
    client = FakeImap({b"1": build_email(attachments=(("IMG-20240115-WA0001.jpg", b"\xff\xd8jpeg"),))})

    report = ingest.ingest_once(config, storage, conn, client)

    assert report.ingested == ["<msg-1@gmx.com>"]
    directory = storage.inbox_dir(slug_for("<msg-1@gmx.com>"))
    assert (directory / "raw.eml").exists()
    assert (directory / "attachments" / "IMG-20240115-WA0001.jpg").read_bytes() == b"\xff\xd8jpeg"
    assert db.get(conn, "<msg-1@gmx.com>")["state"] == db.STATE_INGESTED


def test_rerun_is_a_noop(config: Config, storage: Storage, conn) -> None:
    client = FakeImap({b"1": build_email()})
    ingest.ingest_once(config, storage, conn, client)
    second = ingest.ingest_once(config, storage, conn, client)

    assert second.ingested == []
    assert second.duplicates == ["<msg-1@gmx.com>"]


def test_sender_not_on_allowlist_is_left_untouched(
    config: Config, storage: Storage, conn
) -> None:
    client = FakeImap({b"1": build_email(sender="stranger@spam.invalid")})

    report = ingest.ingest_once(config, storage, conn, client)

    assert report.rejected == ["<msg-1@gmx.com>"]
    assert list(storage.inbox.glob("*")) == []
    assert db.get(conn, "<msg-1@gmx.com>") is None
    assert client.deleted == []


def test_dry_run_writes_nothing(config: Config, storage: Storage, conn) -> None:
    client = FakeImap({b"1": build_email()})

    ingest.ingest_once(config, storage, conn, client, dry_run=True)

    assert list(storage.inbox.glob("*")) == []
    assert db.get(conn, "<msg-1@gmx.com>") is None


def test_missing_message_id_still_dedupes(config: Config, storage: Storage, conn) -> None:
    raw = build_email()
    stripped = email.message_from_bytes(raw, policy=email.policy.default)
    del stripped["Message-ID"]
    client = FakeImap({b"1": stripped.as_bytes()})

    first = ingest.ingest_once(config, storage, conn, client)
    second = ingest.ingest_once(config, storage, conn, client)

    assert len(first.ingested) == 1
    assert second.duplicates == first.ingested


def test_purge_refuses_until_message_is_committed(
    config: Config, storage: Storage, conn
) -> None:
    client = FakeImap({b"1": build_email()})
    ingest.ingest_once(config, storage, conn, client)
    slug = slug_for("<msg-1@gmx.com>")

    # Still only ingested: nothing may leave the server.
    assert ingest.purge_committed(config, storage, conn, client) == []
    assert client.deleted == []

    storage.to_staging(slug)
    storage.to_done(slug)
    with db.transaction(conn):
        db.set_state(conn, "<msg-1@gmx.com>", db.STATE_COMMITTED)

    assert ingest.purge_committed(config, storage, conn, client) == ["<msg-1@gmx.com>"]
    assert client.deleted == [b"1"]
    assert client.expunged


def test_purge_refuses_when_the_eml_is_missing_on_disk(
    config: Config, storage: Storage, conn
) -> None:
    client = FakeImap({b"1": build_email()})
    ingest.ingest_once(config, storage, conn, client)
    slug = slug_for("<msg-1@gmx.com>")
    storage.to_staging(slug)
    storage.to_done(slug)
    (storage.done_dir(slug) / "raw.eml").unlink()
    with db.transaction(conn):
        db.set_state(conn, "<msg-1@gmx.com>", db.STATE_COMMITTED)

    assert ingest.purge_committed(config, storage, conn, client) == []
    assert client.deleted == []


def test_attachment_filenames_cannot_escape_the_message_dir(
    config: Config, storage: Storage, conn, tmp_path: Path
) -> None:
    client = FakeImap({b"1": build_email(attachments=(("../../escape.jpg", b"x"),))})

    ingest.ingest_once(config, storage, conn, client)

    directory = storage.inbox_dir(slug_for("<msg-1@gmx.com>"))
    assert [p.name for p in (directory / "attachments").iterdir()] == ["escape.jpg"]
    assert not (tmp_path / "escape.jpg").exists()
