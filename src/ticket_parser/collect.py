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
import tempfile
import time
import zipfile
from pathlib import Path

from . import db, documents, markdown_export
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
        document_text=str(record.get("document_text") or ""),
        note=str(record.get("note") or ""),
        # Filled in by an earlier `transcribe` pass and persisted to disk, so a
        # replayed or re-collected message does not pay to be transcribed twice.
        transcript=str(record.get("transcript") or ""),
        transcript_error=str(record.get("transcript_error") or ""),
        source_dir=directory,
        asset_dir=directory,
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

    unread = [m for m in messages if m.document_name and not m.document_text]
    if unread:
        names = ", ".join(sorted({m.document_name for m in unread}))
        warnings.append(
            f"{len(unread)} document(s) were attached but their contents are not "
            f"included: {names}. Download them in WhatsApp before exporting "
            "(scanned PDFs have no text to include)."
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
    # Each attached document becomes parts that get their own ids, so the
    # clustering pass can put a passage of a document on the same issue as
    # the chat messages it relates to.
    fresh = documents.expand_documents(fresh)
    for index, message in enumerate(fresh, start=1):
        message.seq = index

    undated = [m for m in fresh if not m.timestamp]
    if undated:
        warnings.append(
            f"{len(undated)} message(s) carried no date at all (typically a "
            "document or voice note at the very top of the export); they are "
            "shown as 'date unknown'."
        )

    batch_id = unique_batch_id(storage, batch_id_for(fresh, now=now))
    return Batch(batch_id=batch_id, messages=fresh, warnings=warnings)


def batch_id_for(messages: list[ChatMessage], *, now: float | None = None) -> str:
    """Name a batch after its earliest *dated* message.

    A message with no date has timestamp 0; letting it win the min() would name
    the batch after 1 Jan 1970, and every such batch would share that name.
    """
    dated = [m.timestamp for m in messages if m.timestamp > 0]
    started = min(dated) if dated else (time.time() if now is None else now)
    return time.strftime("%Y%m%dT%H%M%S", time.localtime(started))


def unique_batch_id(storage: Storage, batch_id: str) -> str:
    """Suffix the id if an earlier batch already used it.

    Two batches sharing a name would merge into one done/ directory and the
    second PDF would overwrite the first.
    """
    candidate = batch_id
    suffix = 2
    while (storage.done / f"batch-{candidate}").exists() or (
        storage.out / f"{candidate}-report.pdf"
    ).exists():
        candidate = f"{batch_id}-{suffix}"
        suffix += 1
    return candidate


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


def _inside(root: Path, relative: str) -> Path | None:
    """`root/relative`, or None if an edited link tries to point outside root."""
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def import_markdown_export(
    storage: Storage,
    md_path: Path,
    *,
    document_dirs: tuple[Path, ...] = (),
    exclude: tuple[Path, ...] = (),
) -> tuple[int, int]:
    """Import one (possibly hand-edited) conversation.md into inbox/.

    Media links are resolved relative to the Markdown file. Documents the
    extension could not include are first looked for in `document_dirs` and
    written into the export (markdown_export.fill_missing_documents). Returns
    (imported, skipped_already_present).
    """
    if document_dirs:
        try:
            filled = markdown_export.fill_missing_documents(md_path, document_dirs, exclude=exclude)
            if filled.filled or filled.no_text:
                log.info("documents filled in from disk", extra={"filled": filled.filled + filled.no_text})
            if filled.not_found:
                log.warning("documents still missing", extra={"names": filled.not_found})
        except OSError as exc:
            log.error("could not fill in documents", extra={"export": str(md_path), "error": str(exc)})
    try:
        export = markdown_export.parse_markdown(md_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, markdown_export.MarkdownExportError) as exc:
        raise CollectError(f"unreadable export {md_path}: {exc}") from exc

    root = md_path.parent
    storage.ensure()
    imported = 0
    skipped = 0

    for message in export.messages:
        name = safe_filename(message.wa_id, fallback="") or f"msg-{message.timestamp}"
        if (storage.inbox / name).exists() or storage.done_dir(name).exists():
            skipped += 1
            continue
        if not any(
            (message.text, message.note, message.media_path, message.document_path,
             message.document_text, message.media_error)
        ):
            # Everything but the marker was deleted: nothing left to report.
            continue

        pending = storage.new_incoming(name)
        record = message.record()
        record["media"] = None

        if message.media_path:
            source = _inside(root, message.media_path)
            if source is not None and source.is_file():
                filename = safe_filename(f"{name}{source.suffix}", fallback=f"{name}.bin")
                shutil.copy2(source, pending / filename)
                record["media"] = {
                    "filename": filename,
                    "mimetype": markdown_export.guess_mimetype(source),
                }
            else:
                record["media_error"] = record["media_error"] or (
                    f"attachment {message.media_path} is missing from the export"
                )

        figures: list[str] = []
        for relative in message.document_figures:
            source = _inside(root, relative)
            if source is not None and source.is_file():
                shutil.copy2(source, pending / source.name)
                figures.append(source.name)
            else:
                log.warning("document figure missing from the export", extra={"file": relative})
        record["document_images"] = figures

        if message.document_path:
            source = _inside(root, message.document_path)
            if source is not None and source.is_file():
                filename = safe_filename(source.name, fallback=f"{name}.bin")
                shutil.copy2(source, pending / filename)
                record["document_file"] = filename

        write_atomic(
            pending / "message.json",
            json.dumps(record, indent=2, ensure_ascii=False).encode("utf-8"),
        )
        storage.commit_incoming(pending, name)
        imported += 1

    log.info(
        "markdown export imported",
        extra={"export": str(md_path), "imported": imported, "skipped": skipped},
    )
    return imported, skipped


def _mark_imported(md_path: Path) -> None:
    """Keep the user's edited file, but make sure it is not imported again."""
    md_path.rename(md_path.with_name(md_path.name + ".imported"))


def import_zip_export(storage: Storage, path: Path, **documents) -> tuple[int, int]:
    """A zipped export: what the phone shares and what email carries.

    A zip is transport, not a working copy, so it is imported whatever its
    status says -- unzip it first if you want to edit it.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="ticket-parser-") as tmp:
            with zipfile.ZipFile(path) as archive:
                # extractall drops absolute paths and '..' components.
                archive.extractall(tmp)
            root = Path(tmp)
            md_path = markdown_export.find_conversation(root)
            if md_path is None:
                subdirs = [d for d in root.iterdir() if d.is_dir()]
                md_path = next(
                    (found for d in subdirs if (found := markdown_export.find_conversation(d))),
                    None,
                )
            if md_path is None:
                raise CollectError(f"{path.name} holds no conversation.md")
            return import_markdown_export(storage, md_path, **documents)
    except zipfile.BadZipFile as exc:
        raise CollectError(f"{path.name} is not a readable zip: {exc}") from exc


def import_path(
    storage: Storage,
    path: Path,
    *,
    include_drafts: bool = False,
    document_dirs: tuple[Path, ...] = (),
    exclude: tuple[Path, ...] = (),
) -> tuple[int, int]:
    """Import whatever `path` is: a drop folder, one export folder, a .md or a .zip.

    A single export named directly is imported whatever its status; a drop
    folder is scanned with `include_drafts` deciding about drafts.
    """
    documents = {"document_dirs": document_dirs, "exclude": exclude}
    if path.is_file():
        if path.suffix.lower() == ".zip":
            return import_zip_export(storage, path, **documents)
        if path.suffix.lower() == ".md":
            imported, skipped = import_markdown_export(storage, path, **documents)
            _mark_imported(path)
            return imported, skipped
        return import_bundle(storage, path)
    md_path = markdown_export.find_conversation(path) if path.is_dir() else None
    if md_path is not None:
        imported, skipped = import_markdown_export(storage, md_path, **documents)
        _mark_imported(md_path)
        return imported, skipped
    return import_drop(storage, path, include_drafts=include_drafts, **documents)


def import_drop(
    storage: Storage,
    drop_dir: Path,
    *,
    include_drafts: bool = True,
    document_dirs: tuple[Path, ...] = (),
    exclude: tuple[Path, ...] = (),
) -> tuple[int, int]:
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
        # Markdown exports: a folder holding conversation.md + media/, or a
        # bare .md. A draft is still being edited; `watch` leaves it alone.
        md_path = None
        if entry.is_dir() and not (entry / "message.json").is_file():
            md_path = markdown_export.find_conversation(entry)
        elif entry.is_file() and entry.suffix.lower() == ".md" and markdown_export.looks_like_export(entry):
            md_path = entry
        if md_path is not None:
            try:
                export = markdown_export.parse_markdown(md_path.read_text(encoding="utf-8"))
                if not include_drafts and not export.ready:
                    log.info("export is still a draft, leaving it", extra={"export": str(md_path)})
                    continue
                added, already = import_markdown_export(
                    storage, md_path, document_dirs=document_dirs, exclude=exclude
                )
            except (OSError, UnicodeDecodeError, markdown_export.MarkdownExportError, CollectError) as exc:
                log.error("skipping bad export", extra={"export": str(md_path), "error": str(exc)})
                continue
            imported += added
            skipped += already
            _mark_imported(md_path)
            continue

        if entry.is_file() and entry.suffix.lower() == ".zip":
            try:
                added, already = import_zip_export(
                    storage, entry, document_dirs=document_dirs, exclude=exclude
                )
            except CollectError as exc:
                log.error("skipping bad zip export", extra={"error": str(exc)})
                continue
            imported += added
            skipped += already
            entry.unlink()
            continue

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
