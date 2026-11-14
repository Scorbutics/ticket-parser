"""Collect bundles that arrive as email attachments.

This is the mobile leg: you export from Firefox on the phone, share the bundle
to your mail app, and it lands here. The IMAP client, sender allowlist and
Message-ID dedupe are reused from `ingest`, which was written and tested for
the original mail-based design.

Safety properties carried over unchanged:
  - only mail from an allowlisted sender is touched;
  - dedupe on Message-ID, so re-running is a no-op;
  - nothing is ever deleted from the server (attachments here are copies of
    data you still hold on the phone, so deletion buys nothing and risks
    losing a report).
"""

from __future__ import annotations

import email
import email.policy
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .config import Config
from .ingest import ImapClient, IngestError, _decode, sender_allowed
from .storage import safe_filename, write_atomic

log = logging.getLogger(__name__)

BUNDLE_SUFFIXES = (".json.gz", ".json", ".zip")


@dataclass
class MailReport:
    scanned: int = 0
    bundles: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def looks_like_bundle(filename: str) -> bool:
    return filename.lower().endswith(BUNDLE_SUFFIXES)


def fetch_bundles(
    config: Config,
    conn: sqlite3.Connection,
    client: ImapClient,
    drop_dir: Path,
    *,
    dry_run: bool = False,
) -> MailReport:
    """Save bundle attachments from allowlisted mail into the drop folder."""
    report = MailReport()
    drop_dir.mkdir(parents=True, exist_ok=True)

    for uid in client.search_uids():
        try:
            raw = client.fetch(uid)
        except IngestError as exc:
            report.errors.append(str(exc))
            log.error("fetch failed", extra={"uid": uid.decode(), "error": str(exc)})
            continue

        report.scanned += 1
        message = email.message_from_bytes(raw, policy=email.policy.default)
        message_id = (message.get("Message-ID") or "").strip()
        from_header = _decode(message.get("From"))

        if not message_id:
            import hashlib

            message_id = f"<sha256-{hashlib.sha256(raw).hexdigest()[:32]}@local>"

        if not sender_allowed(from_header, config.allowed_senders):
            report.rejected.append(message_id)
            log.warning(
                "sender not allowlisted, leaving untouched",
                extra={"message_id": message_id, "from": from_header},
            )
            continue

        if db.is_known(conn, message_id):
            report.duplicates.append(message_id)
            continue

        saved: list[str] = []
        for part in message.walk():
            filename = _decode(part.get_filename())
            if not filename or not looks_like_bundle(filename):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            target = drop_dir / safe_filename(filename, fallback="bundle.json.gz")
            if target.exists():
                target = target.with_name(f"{message_id[1:9]}-{target.name}")
            if dry_run:
                log.info("dry-run: would save", extra={"file": target.name})
            else:
                write_atomic(target, payload)
            saved.append(target.name)

        if not saved:
            log.info(
                "no bundle attached, ignoring",
                extra={"message_id": message_id, "subject": _decode(message.get("Subject"))},
            )
            continue

        if not dry_run:
            # Recorded as committed straight away: the payload is in the drop
            # folder, and this row exists only to stop us re-reading the mail.
            with db.transaction(conn):
                db.record_ingested(
                    conn,
                    message_id=message_id,
                    slug=safe_filename(message_id, fallback="mail"),
                    uid=uid.decode(),
                    from_addr=from_header,
                    subject=_decode(message.get("Subject")),
                    date_header=_decode(message.get("Date")),
                )
                db.set_state(conn, message_id, db.STATE_COMMITTED)

        report.bundles.extend(saved)
        log.info(
            "bundle saved from mail",
            extra={"message_id": message_id, "files": saved},
        )

    return report


def poll(
    config: Config, conn: sqlite3.Connection, *, dry_run: bool = False
) -> MailReport:
    """One mailbox pass. Connection errors are the caller's to handle."""
    with ImapClient(config) as client:
        return fetch_bundles(config, conn, client, config.drop_dir, dry_run=dry_run)
