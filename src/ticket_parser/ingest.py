"""IMAP ingestion: fetch, allowlist, dedupe, durable write, and only then delete.

Ordering rule that everything else depends on: a message is never removed from
the server until its .eml and every attachment are committed under ``done/``.
Deletion is a separate pass (`purge`) over already-committed messages.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import json
import logging
import socket
import sqlite3
import ssl
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

from . import db
from .config import Config
from .storage import Storage, safe_filename, write_atomic

log = logging.getLogger(__name__)

MAX_IDLE_SECONDS = 25 * 60  # servers drop IDLE well before 30 minutes


class IngestError(RuntimeError):
    pass


@dataclass
class IngestedMessage:
    message_id: str
    slug: str
    path: Path
    from_addr: str
    subject: str
    date_header: str
    attachments: list[str] = field(default_factory=list)


@dataclass
class IngestReport:
    fetched: int = 0
    ingested: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # sender not allowlisted
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# IMAP


class ImapClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "ImapClient":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def conn(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            raise IngestError("not connected")
        return self._conn

    def connect(self) -> None:
        cfg = self.config
        cfg.require_imap()
        context = ssl.create_default_context()
        log.info("imap connect", extra={"host": cfg.imap_host, "port": cfg.imap_port})
        conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=context)
        try:
            conn.login(cfg.imap_user, cfg.imap_password)
        except imaplib.IMAP4.error as exc:
            raise IngestError(
                "IMAP login failed. GMX rejects IMAP until it is enabled in the web "
                f"settings (Home > Settings > POP3/IMAP). Server said: {exc}"
            ) from exc
        conn.select(cfg.imap_folder)
        self._conn = conn

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
            self._conn.logout()
        except Exception:  # a dead connection is fine to drop on the floor
            pass
        finally:
            self._conn = None

    def reconnect(self) -> None:
        self.close()
        self.connect()

    # UID commands throughout, never sequence numbers: a UID stays valid across
    # sessions, which is what lets `purge` delete a message in a later run than
    # the one that ingested it.
    def search_uids(self) -> list[bytes]:
        typ, data = self.conn.uid("SEARCH", None, "ALL")
        if typ != "OK":
            raise IngestError(f"IMAP SEARCH failed: {typ}")
        return data[0].split() if data and data[0] else []

    def fetch(self, uid: bytes) -> bytes:
        typ, data = self.conn.uid("FETCH", uid, "(RFC822)")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            raise IngestError(f"IMAP FETCH failed for uid {uid!r}: {typ}")
        return data[0][1]

    def delete(self, uid: bytes) -> None:
        typ, _ = self.conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        if typ != "OK":
            raise IngestError(f"IMAP STORE \\Deleted failed for uid {uid!r}: {typ}")

    def expunge(self) -> None:
        self.conn.expunge()

    def idle(self, timeout: int = MAX_IDLE_SECONDS) -> bool:
        """Block until the server reports activity. True if something happened.

        imaplib has no IDLE support, so this drives the command over the socket
        directly. Any protocol hiccup returns False and the caller reconnects --
        IMAP connections die silently and a stuck watcher is the failure mode
        this whole function exists to avoid.
        """
        conn = self.conn
        tag = conn._new_tag()  # type: ignore[attr-defined]
        try:
            conn.send(b"%s IDLE\r\n" % tag)
            conn.readline()  # server's '+ idling'
            conn.sock.settimeout(timeout)  # type: ignore[union-attr]
            try:
                line = conn.readline()
                activity = bool(line.strip())
            except (socket.timeout, TimeoutError):
                activity = False
            conn.send(b"DONE\r\n")
            conn.sock.settimeout(None)  # type: ignore[union-attr]
            while True:
                line = conn.readline()
                if not line or line.startswith(tag):
                    break
            return activity
        except (OSError, imaplib.IMAP4.abort, imaplib.IMAP4.error) as exc:
            log.warning("idle failed, will reconnect", extra={"error": str(exc)})
            return False


# --------------------------------------------------------------------------
# Ingestion


def sender_allowed(from_header: str, allowed: tuple[str, ...]) -> bool:
    addr = parseaddr(from_header or "")[1].strip().lower()
    return bool(addr) and addr in allowed


def _decode(value: str | None) -> str:
    if not value:
        return ""
    from email.header import decode_header, make_header

    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def write_message(
    storage: Storage, raw: bytes, msg: EmailMessage, slug: str
) -> IngestedMessage:
    """Materialise one email into ``inbox/<slug>/`` atomically.

    Everything lands in a hidden ``.partial`` dir first; the final rename is the
    single instant at which the message becomes visible as ingested.
    """
    pending = storage.new_incoming(slug)
    write_atomic(pending / "raw.eml", raw)

    attachments: list[str] = []
    attachments_dir = pending / "attachments"
    attachments_dir.mkdir()
    seen: set[str] = set()

    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        disposition = (part.get_content_disposition() or "").lower()
        if not filename and disposition != "attachment":
            continue
        name = safe_filename(_decode(filename), fallback="attachment.bin")
        stem, dot, ext = name.rpartition(".")
        counter = 1
        while name in seen:
            name = f"{stem}-{counter}{dot}{ext}" if dot else f"{name}-{counter}"
            counter += 1
        seen.add(name)
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        write_atomic(attachments_dir / name, payload)
        attachments.append(name)

    from_addr = _decode(msg.get("From"))
    subject = _decode(msg.get("Subject"))
    date_header = _decode(msg.get("Date"))
    meta = {
        "message_id": msg.get("Message-ID", "").strip(),
        "slug": slug,
        "from": from_addr,
        "subject": subject,
        "date": date_header,
        "attachments": attachments,
        "ingested_at": time.time(),
    }
    write_atomic(
        pending / "meta.json",
        json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"),
    )

    path = storage.commit_incoming(pending, slug)
    return IngestedMessage(
        message_id=meta["message_id"],
        slug=slug,
        path=path,
        from_addr=from_addr,
        subject=subject,
        date_header=date_header,
        attachments=attachments,
    )


def ingest_once(
    config: Config,
    storage: Storage,
    conn: sqlite3.Connection,
    client: ImapClient,
    *,
    dry_run: bool = False,
) -> IngestReport:
    from .storage import slug_for

    report = IngestReport()
    storage.ensure()

    for uid in client.search_uids():
        try:
            raw = client.fetch(uid)
        except IngestError as exc:
            report.errors.append(str(exc))
            log.error("fetch failed", extra={"uid": uid.decode(), "error": str(exc)})
            continue

        report.fetched += 1
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        message_id = (msg.get("Message-ID") or "").strip()
        from_header = _decode(msg.get("From"))

        if not message_id:
            # No Message-ID means no dedupe key; synthesise a stable one from
            # the bytes so a re-run is still a no-op.
            import hashlib

            message_id = f"<sha256-{hashlib.sha256(raw).hexdigest()[:32]}@local>"
            log.warning("message has no Message-ID, synthesised", extra={"message_id": message_id})

        if not sender_allowed(from_header, config.allowed_senders):
            report.rejected.append(message_id)
            log.warning(
                "sender not allowlisted, leaving untouched",
                extra={"message_id": message_id, "from": from_header},
            )
            continue

        if db.is_known(conn, message_id):
            report.duplicates.append(message_id)
            log.info("duplicate, skipping", extra={"message_id": message_id})
            continue

        if dry_run:
            report.ingested.append(message_id)
            log.info("dry-run: would ingest", extra={"message_id": message_id})
            continue

        slug = slug_for(message_id)
        ingested = write_message(storage, raw, msg, slug)
        with db.transaction(conn):
            db.record_ingested(
                conn,
                message_id=message_id,
                slug=slug,
                uid=uid.decode(),
                from_addr=ingested.from_addr,
                subject=ingested.subject,
                date_header=ingested.date_header,
            )
        report.ingested.append(message_id)
        log.info(
            "ingested",
            extra={
                "message_id": message_id,
                "slug": slug,
                "attachments": len(ingested.attachments),
            },
        )

    backlog = db.count_unprocessed(conn)
    if backlog > config.backlog_warn_threshold:
        log.warning(
            "unprocessed backlog above threshold -- the pipeline has been failing",
            extra={"backlog": backlog, "threshold": config.backlog_warn_threshold},
        )
    return report


def purge_committed(
    config: Config,
    storage: Storage,
    conn: sqlite3.Connection,
    client: ImapClient,
    *,
    dry_run: bool = False,
) -> list[str]:
    """Delete from the server only what is provably committed on disk."""
    deleted: list[str] = []
    for row in db.list_by_state(conn, db.STATE_COMMITTED):
        done_dir = storage.done_dir(row["slug"])
        if not (done_dir / "raw.eml").is_file():
            log.error(
                "committed row without raw.eml on disk, refusing to delete",
                extra={"message_id": row["message_id"], "path": str(done_dir)},
            )
            continue
        if row["uid"] is None:
            continue
        if dry_run:
            log.info("dry-run: would delete", extra={"message_id": row["message_id"]})
            continue
        client.delete(row["uid"].encode())
        with db.transaction(conn):
            db.set_state(conn, row["message_id"], db.STATE_DELETED)
        deleted.append(row["message_id"])
        log.info("deleted from server", extra={"message_id": row["message_id"]})
    if deleted and not dry_run:
        client.expunge()
    return deleted
