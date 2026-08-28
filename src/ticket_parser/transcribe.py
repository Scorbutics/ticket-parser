"""Speech-to-text for voice notes, run once per audio file and cached.

Runs entirely offline via faster-whisper (MIT licensed, no API key, no network
call) -- an intentional match for the rest of the pipeline's "a model failure
never costs you the document" policy: a transcription failure leaves the voice
note in the report with no transcript and a note on the page, never a missing
message.

The transcript is written back into the message's still-in-inbox message.json
right after it is computed, so it travels into done/ with the audio file when
the batch is committed -- the same "every stage is inspectable on disk"
discipline storage.py follows everywhere else -- and a re-collected message
never pays to be transcribed twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from sqlite3 import Connection

from . import db
from .config import Config
from .models import Batch, ChatMessage
from .storage import write_atomic

log = logging.getLogger(__name__)

PROMPT_VERSION = "transcribe-v1"

# One model per process: loading the weights is the expensive part, reusing
# it across every voice note in a batch is what keeps a batch fast.
_model = None
_model_key: tuple[str, str, str] | None = None


class TranscribeError(RuntimeError):
    pass


def _load_model(model_size: str, device: str, compute_type: str):
    global _model, _model_key
    key = (model_size, device, compute_type)
    if _model is not None and _model_key == key:
        return _model
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscribeError(
            "faster-whisper is not installed; run "
            "`pip install -e '.[transcribe]'` to enable voice note transcripts"
        ) from exc
    _model = WhisperModel(model_size, device=device, compute_type=compute_type)
    _model_key = key
    return _model


def transcribe_file(
    path: Path, *, model_size: str, device: str, compute_type: str
) -> str:
    model = _load_model(model_size, device, compute_type)
    segments, _info = model.transcribe(str(path))
    return " ".join(segment.text.strip() for segment in segments).strip()


def cache_key(file_hash: str, model_size: str) -> str:
    payload = "\x1f".join((file_hash, PROMPT_VERSION, model_size))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _persist(message: ChatMessage) -> None:
    """Write the transcript back into message.json, next to the audio file."""
    if message.source_dir is None:
        return
    record_path = message.source_dir / "message.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error(
            "could not persist transcript",
            extra={"message_id": message.wa_id, "error": str(exc)},
        )
        return
    record["transcript"] = message.transcript
    record["transcript_error"] = message.transcript_error
    write_atomic(
        record_path, json.dumps(record, indent=2, ensure_ascii=False).encode("utf-8")
    )


def transcribe_batch(batch: Batch, config: Config, conn: Connection) -> Batch:
    """Fill in `message.transcript` for every voice note in the batch.

    Never fails the batch: a missing model, a bad audio file, or a model that
    finds no speech leaves the voice note in the report with
    `transcript_error` set instead of raising.
    """
    if not config.transcribe_enabled:
        return batch

    voice_messages = [
        m
        for m in batch.messages
        if m.media is not None
        and m.media.is_audio
        and not m.transcript
        and not m.transcript_error
    ]
    if not voice_messages:
        return batch

    failures = 0
    for message in voice_messages:
        assert message.media is not None
        try:
            file_hash = _file_hash(message.media.path)
        except OSError as exc:
            message.transcript_error = f"could not read the audio file: {exc}"
            _persist(message)
            failures += 1
            continue

        key = cache_key(file_hash, config.whisper_model)
        cached = db.cache_get(conn, key)
        if cached is not None:
            message.transcript = cached
            _persist(message)
            continue

        try:
            text = transcribe_file(
                message.media.path,
                model_size=config.whisper_model,
                device=config.whisper_device,
                compute_type=config.whisper_compute_type,
            )
        except Exception as exc:  # a bad model or file must never cost the report
            log.error(
                "transcription failed",
                extra={"message_id": message.wa_id, "error": str(exc)},
            )
            message.transcript_error = f"could not transcribe voice note: {exc}"
            failures += 1
            _persist(message)
            continue

        message.transcript = text
        if not text:
            message.transcript_error = "transcription returned no speech"
        with db.transaction(conn):
            db.cache_put(
                conn,
                cache_key=key,
                message_id=batch.batch_id,
                issue_key=message.wa_id or str(message.seq),
                repo_head="",
                prompt_version=PROMPT_VERSION,
                payload_json=text,
            )
        _persist(message)

    if failures:
        batch.warnings.append(
            f"{failures} voice note(s) could not be transcribed; the audio is "
            "intact in the report but has no transcript."
        )
    return batch
