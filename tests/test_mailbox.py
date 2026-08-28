"""The mail leg: bundles arriving as email attachments from the phone."""

from __future__ import annotations

import gzip
import json
from email.message import EmailMessage
from pathlib import Path

import pytest

from ticket_parser import collect, db, mailbox
from ticket_parser.config import Config
from ticket_parser.storage import Storage

from test_bundle import message


@pytest.fixture
def conn(config: Config):
    return db.connect(config.db_path)


def bundle_bytes(messages: list[dict]) -> bytes:
    return gzip.compress(
        json.dumps({"schema": 1, "messages": messages}).encode("utf-8")
    )


def build_mail(
    *,
    message_id: str = "<phone-1@gmx.com>",
    sender: str = "me@example.invalid",
    attachment: tuple[str, bytes] | None = ("export.json.gz", None),
) -> bytes:
    mail = EmailMessage()
    mail["Message-ID"] = message_id
    mail["From"] = sender
    mail["Subject"] = "WhatsApp bug reports"
    mail["Date"] = "Thu, 13 Aug 2026 19:00:00 +0200"
    mail.set_content("sent from my phone")
    if attachment:
        name, payload = attachment
        if payload is None:
            payload = bundle_bytes([message("false_x_A", "le login est cassé", 1)])
        mail.add_attachment(
            payload, maintype="application", subtype="gzip", filename=name
        )
    return mail.as_bytes()


class FakeImap:
    def __init__(self, messages: dict[bytes, bytes]) -> None:
        self.messages = messages
        self.deleted: list[bytes] = []

    def search_uids(self) -> list[bytes]:
        return list(self.messages)

    def fetch(self, uid: bytes) -> bytes:
        return self.messages[uid]

    def delete(self, uid: bytes) -> None:  # pragma: no cover - must never run
        self.deleted.append(uid)

    def expunge(self) -> None:  # pragma: no cover
        pass


def test_bundle_attachment_lands_in_the_drop_folder(
    config: Config, conn, tmp_path: Path
) -> None:
    client = FakeImap({b"1": build_mail()})

    report = mailbox.fetch_bundles(config, conn, client, config.drop_dir)

    assert report.bundles == ["export.json.gz"]
    assert (config.drop_dir / "export.json.gz").is_file()


def test_mail_is_never_deleted_from_the_server(
    config: Config, conn, tmp_path: Path
) -> None:
    client = FakeImap({b"1": build_mail()})

    mailbox.fetch_bundles(config, conn, client, config.drop_dir)

    assert client.deleted == []


def test_rerunning_does_not_fetch_the_same_mail_twice(config: Config, conn) -> None:
    client = FakeImap({b"1": build_mail()})
    mailbox.fetch_bundles(config, conn, client, config.drop_dir)

    second = mailbox.fetch_bundles(config, conn, client, config.drop_dir)

    assert second.bundles == []
    assert second.duplicates == ["<phone-1@gmx.com>"]


def test_mail_from_a_stranger_is_ignored(config: Config, conn) -> None:
    client = FakeImap({b"1": build_mail(sender="stranger@spam.invalid")})

    report = mailbox.fetch_bundles(config, conn, client, config.drop_dir)

    assert report.rejected == ["<phone-1@gmx.com>"]
    assert not config.drop_dir.exists() or list(config.drop_dir.iterdir()) == []


def test_mail_without_a_bundle_is_ignored(config: Config, conn) -> None:
    client = FakeImap({b"1": build_mail(attachment=None)})

    report = mailbox.fetch_bundles(config, conn, client, config.drop_dir)

    assert report.bundles == []
    assert report.scanned == 1
    # Not recorded as seen: a later mail with the same id could carry a bundle.
    assert db.get(conn, "<phone-1@gmx.com>") is None


def test_unrelated_attachments_are_not_picked_up(config: Config, conn) -> None:
    client = FakeImap({b"1": build_mail(attachment=("holiday.jpg", b"\xff\xd8"))})

    report = mailbox.fetch_bundles(config, conn, client, config.drop_dir)

    assert report.bundles == []


def test_dry_run_writes_nothing(config: Config, conn) -> None:
    client = FakeImap({b"1": build_mail()})

    mailbox.fetch_bundles(config, conn, client, config.drop_dir, dry_run=True)

    assert not (config.drop_dir / "export.json.gz").exists()
    assert db.get(conn, "<phone-1@gmx.com>") is None


def test_mail_to_pdf_end_to_end(
    config: Config, storage: Storage, conn, tmp_path: Path
) -> None:
    import time

    from ticket_parser import enrich, render

    now = time.time()
    payload = bundle_bytes(
        [
            message("false_x_A", "le login est cassé", int(now - 9000)),
            message("false_x_B", "et le panier aussi", int(now - 8800)),
        ]
    )
    client = FakeImap({b"1": build_mail(attachment=("export.json.gz", payload))})

    mailbox.fetch_bundles(config, conn, client, config.drop_dir)
    collect.import_drop(storage, config.drop_dir)
    batch = collect.collect_batch(storage, conn, quiet_period_seconds=7200, now=now)

    assert batch is not None
    assert len(batch.messages) == 2

    enrich.cluster(batch, None, enabled=False)
    rendered = render.render_html(batch)
    assert "le login est cassé" in rendered
    assert "et le panier aussi" in rendered
