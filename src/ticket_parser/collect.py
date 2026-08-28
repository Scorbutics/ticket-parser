"""Read exported messages, and close a batch after a quiet period.

There is no WhatsApp chat-log parsing here, and that is the point: the browser
extension delivers each message as its own structured record, so the
locale-dependent timestamp regex and the `<attached: ...>` placeholder matching
that these scripts normally break on simply do not exist in this design.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import shutil
import sqlite3
import time
from pathlib import Path

from . import db
from .models import Batch, ChatMessage, Media
from .storage import Storage, safe_filename, write_atomic

log = logging.getLogger(__name__)


class CollectError(RuntimeError):
    pass


def read_message_dir(directory: Path, seq: int) -> ChatMessage:
    record_path = directory / "message.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectError(f"unreadable message record at {record_path}: {exc}") from exc

    media = None
    media_error = str(record.get("media_error") or "")
    raw_media = record.get("media")
    if raw_media and raw_media.get("filename"):
        media_path = directory / raw_media["filename"]
        if media_path.is_file():
            media = Media(
                filename=raw_media["filename"],
                mimetype=raw_media.get("mimetype", ""),
                path=media_path,
            )
        else:
            media_error = media_error or "attachment missing on disk"
            log.warning(
                "media referenced but missing on disk",
                extra={"message_id": record.get("id"), "file": raw_media["filename"]},
            )

    return ChatMessage(
        seq=seq,
        wa_id=str(record.get("id", "")),
        chat_id=str(record.get("chat_id", "")),
        author_name=str(record.get("author_name") or "unknown"),
        timestamp=int(record.get("timestamp") or 0),
        text=str(record.get("text") or ""),
        media=media,
        media_error=media_error,
        timestamp_inferred=bool(record.get("timestamp_inferred")),
        document_name=str(record.get("document_name") or ""),
        # Filled in by an earlier `transcribe` pass and persisted to disk, so a
        # replayed or re-collected message does not pay to be transcribed twice.
        transcript=str(record.get("transcript") or ""),
        transcript_error=str(record.get("transcript_error") or ""),
        source_dir=directory,
    )


def pending_dirs(storage: Storage) -> list[Path]:
    """Committed message dirs, oldest first. Ignores in-flight .partial dirs."""
    if not storage.inbox.is_dir():
        return []
    dirs = [
        entry
        for entry in storage.inbox.iterdir()
        if entry.is_dir()
        and not entry.name.startswith(".")
        and (entry / "message.json").is_file()
    ]
    return sorted(dirs, key=lambda p: (p / "message.json").stat().st_mtime)


def batch_is_ready(
    messages: list[ChatMessage], quiet_period_seconds: int, now: float | None = None
) -> bool:
    """A burst closes once the chat has been silent for the quiet period."""
    if not messages:
        return False
    now = time.time() if now is None else now
    newest = max(m.timestamp for m in messages)
    return (now - newest) >= quiet_period_seconds


def collect_batch(
    storage: Storage,
    conn: sqlite3.Connection,
    *,
    quiet_period_seconds: int,
    force: bool = False,
    now: float | None = None,
) -> Batch | None:
    """Gather pending messages into a batch, if one is ready to close.

    Returns None when there is nothing pending, when the chat is still active,
    or when every pending message has already been seen in an earlier batch.
    """
    directories = pending_dirs(storage)
    if not directories:
        return None

    messages: list[ChatMessage] = []
    warnings: list[str] = []
    for index, directory in enumerate(directories, start=1):
        try:
            messages.append(read_message_dir(directory, seq=index))
        except CollectError as exc:
            # Never silently drop content: surface it and keep going.
            warnings.append(str(exc))
            log.error("skipping unreadable message", extra={"error": str(exc)})

    if not messages:
        return None

    if not force and not batch_is_ready(messages, quiet_period_seconds, now=now):
        log.info(
            "chat still active, holding batch open",
            extra={"pending": len(messages), "quiet_period": quiet_period_seconds},
        )
        return None

    fingerprints = [m.fingerprint() for m in messages]
    unseen, seen = db.filter_unseen(conn, fingerprints)
    unseen_set = set(unseen)
    fresh = [m for m, fp in zip(messages, fingerprints) if fp in unseen_set]

    if seen:
        log.info("skipping already-triaged messages", extra={"count": len(seen)})

    inferred = [m for m in messages if m.timestamp_inferred]
    if inferred:
        warnings.append(
            f"{len(inferred)} message(s) had their time inferred from the chat "
            "bubble rather than read directly; the date may be wrong across a "
            "midnight boundary."
        )

    documents = [m for m in messages if m.document_name]
    if documents:
        names = ", ".join(sorted({m.document_name for m in documents}))
        warnings.append(
            f"{len(documents)} document(s) were attached and are noted but not "
            f"included: {names}. WhatsApp only releases them on download."
        )

    lost = [m for m in messages if m.media_error and not m.document_name]
    if lost:
        warnings.append(
            f"{len(lost)} message(s) had a screenshot that could not be retrieved; "
            "the text is intact and the gap is marked on the page."
        )
    if not fresh:
        log.info("nothing new in this batch, no PDF will be produced")
        return None

    # Renumber so the ids shown to the model are dense and start at 1.
    fresh.sort(key=lambda m: m.timestamp)
    for index, message in enumerate(fresh, start=1):
        message.seq = index

    started = min(m.timestamp for m in fresh)
    batch_id = time.strftime("%Y%m%dT%H%M%S", time.localtime(started))
    return Batch(batch_id=batch_id, messages=fresh, warnings=warnings)


def import_bundle(storage: Storage, path: Path) -> tuple[int, int]:
    """Unpack one `export.json.gz` written by the extension into inbox/.

    The bundle carries media inline as base64, because it has to survive being
    an email attachment. Here it is split back into the same directory-per-
    message layout everything downstream already reads.
    """
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, EOFError) as exc:
        raise CollectError(f"unreadable bundle {path.name}: {exc}") from exc

    messages = payload.get("messages") if isinstance(payload, dict) else payload
    if not isinstance(messages, list):
        raise CollectError(f"bundle {path.name} has no message list")

    storage.ensure()
    imported = 0
    skipped = 0

    for record in messages:
        if not isinstance(record, dict):
            continue
        raw_id = str(record.get("id") or "")
        name = safe_filename(raw_id, fallback="") or f"msg-{record.get('timestamp', 0)}"
        if (storage.inbox / name).exists() or storage.done_dir(name).exists():
            skipped += 1
            continue

        pending = storage.new_incoming(name)
        media = record.get("media") or {}
        stored_media = None
        if media.get("base64"):
            filename = safe_filename(
                media.get("filename") or f"{name}{media.get('extension', '.bin')}",
                fallback=f"{name}.bin",
            )
            try:
                write_atomic(pending / filename, base64.b64decode(media["base64"]))
                stored_media = {
                    "filename": filename,
                    "mimetype": media.get("mimetype", ""),
                }
            except (ValueError, OSError) as exc:
                log.error(
                    "could not decode bundled media",
                    extra={"message_id": raw_id, "error": str(exc)},
                )
                record["media_error"] = f"attachment could not be decoded: {exc}"

        # Strip the base64 payload: the file on disk is now the source of truth.
        record = {**record, "media": stored_media}
        write_atomic(
            pending / "message.json",
            json.dumps(record, indent=2, ensure_ascii=False).encode("utf-8"),
        )
        storage.commit_incoming(pending, name)
        imported += 1

    log.info(
        "bundle imported",
        extra={"bundle": path.name, "imported": imported, "skipped": skipped},
    )
    return imported, skipped


def import_drop(storage: Storage, drop_dir: Path) -> tuple[int, int]:
    """Move message dirs written by the browser extension into inbox/.

    The extension saves through the downloads API, so its output lands wherever
    Firefox puts downloads. This lifts it into the pipeline's own layout without
    the extension needing to know anything about it.

    Returns (imported, skipped_already_present).
    """
    if not drop_dir.is_dir():
        raise CollectError(f"drop directory does not exist: {drop_dir}")

    storage.ensure()
    imported = 0
    skipped = 0

    for entry in sorted(drop_dir.iterdir()):
        # Bundles: one gzipped file holding a whole conversation. This is what
        # arrives from the phone, by share sheet or as an email attachment.
        if entry.is_file() and entry.name.endswith((".json.gz", ".json")):
            try:
                added, already = import_bundle(storage, entry)
            except CollectError as exc:
                log.error("skipping bad bundle", extra={"error": str(exc)})
                continue
            imported += added
            skipped += already
            entry.unlink()
            continue

        if not entry.is_dir() or not (entry / "message.json").is_file():
            continue

        target = storage.inbox / entry.name
        if target.exists() or storage.done_dir(entry.name).exists():
            skipped += 1
            continue

        # Same rename-into-place discipline as everywhere else: stage under a
        # hidden name, then make it visible in one atomic step.
        pending = storage.new_incoming(entry.name)
        for item in entry.iterdir():
            if item.is_file():
                shutil.copy2(item, pending / item.name)
        storage.commit_incoming(pending, entry.name)
        shutil.rmtree(entry)
        imported += 1
        log.info("imported message from drop", extra={"message_id": entry.name})

    log.info("drop import complete", extra={"imported": imported, "skipped": skipped})
    return imported, skipped


def commit_batch(storage: Storage, conn: sqlite3.Connection, batch: Batch) -> Path:
    """Move a batch's message dirs into done/ and record its messages as seen."""
    target = storage.done / f"batch-{batch.batch_id}"
    target.mkdir(parents=True, exist_ok=True)
    for message in batch.messages:
        if message.source_dir and message.source_dir.exists():
            destination = target / message.source_dir.name
            if not destination.exists():
                message.source_dir.rename(destination)
            message.source_dir = destination
    with db.transaction(conn):
        db.mark_seen(
            conn, [m.fingerprint() for m in batch.messages], f"batch-{batch.batch_id}"
        )
    return target
