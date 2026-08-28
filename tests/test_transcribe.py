"""Voice-note transcription: caching, disk persistence, and failure policy.

A real Whisper model is never loaded here -- `transcribe_file` is monkeypatched
so these tests run fast and offline. What they actually check is the contract
around it: the transcript lands in the message, is written back to
message.json next to the audio, is cached so the same file is never
transcribed twice, and a failure degrades gracefully instead of raising.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ticket_parser import collect, db, transcribe
from ticket_parser.config import Config
from ticket_parser.models import Batch, ChatMessage, Media


@pytest.fixture
def conn(config: Config):
    return db.connect(config.db_path)


def make_voice_message(tmp_path: Path, *, seq: int = 1, audio: bytes = b"fake-ogg") -> ChatMessage:
    source_dir = tmp_path / f"msg-{seq}"
    source_dir.mkdir()
    audio_path = source_dir / "voice.ogg"
    audio_path.write_bytes(audio)
    (source_dir / "message.json").write_text(
        json.dumps(
            {
                "id": f"voice-{seq}",
                "chat_id": "chat",
                "author_name": "Product Owner",
                "timestamp": 1_700_000_000,
                "text": "",
                "media": {"filename": "voice.ogg", "mimetype": "audio/ogg"},
            }
        ),
        encoding="utf-8",
    )
    return ChatMessage(
        seq=seq,
        wa_id=f"voice-{seq}",
        chat_id="chat",
        author_name="Product Owner",
        timestamp=1_700_000_000,
        text="",
        media=Media("voice.ogg", "audio/ogg", audio_path),
        source_dir=source_dir,
    )


def test_transcribe_batch_fills_transcript_and_persists_to_disk(
    config: Config, conn, tmp_path: Path, monkeypatch
) -> None:
    message = make_voice_message(tmp_path)
    batch = Batch(batch_id="20260101T0000", messages=[message])
    config = config.__class__(**{**config.__dict__, "transcribe_enabled": True})

    monkeypatch.setattr(
        transcribe, "transcribe_file", lambda path, **kw: "le bouton ne marche pas"
    )

    transcribe.transcribe_batch(batch, config, conn)

    assert message.transcript == "le bouton ne marche pas"
    assert message.transcript_error == ""
    on_disk = json.loads((message.source_dir / "message.json").read_text())
    assert on_disk["transcript"] == "le bouton ne marche pas"


def test_transcribe_batch_caches_by_file_content(
    config: Config, conn, tmp_path: Path, monkeypatch
) -> None:
    config = config.__class__(**{**config.__dict__, "transcribe_enabled": True})
    calls = []
    monkeypatch.setattr(
        transcribe,
        "transcribe_file",
        lambda path, **kw: calls.append(path) or "hello",
    )

    first = make_voice_message(tmp_path, seq=1)
    transcribe.transcribe_batch(Batch(batch_id="b1", messages=[first]), config, conn)

    # Same audio bytes, a different message/file -- the cache is keyed on
    # content, not on identity, so this must not call the model again.
    second = make_voice_message(tmp_path, seq=2, audio=b"fake-ogg")
    transcribe.transcribe_batch(Batch(batch_id="b2", messages=[second]), config, conn)

    assert len(calls) == 1
    assert second.transcript == "hello"


def test_transcribe_failure_does_not_raise_and_warns(
    config: Config, conn, tmp_path: Path, monkeypatch
) -> None:
    config = config.__class__(**{**config.__dict__, "transcribe_enabled": True})
    message = make_voice_message(tmp_path)
    batch = Batch(batch_id="b1", messages=[message])

    def boom(path, **kw):
        raise RuntimeError("model not installed")

    monkeypatch.setattr(transcribe, "transcribe_file", boom)

    transcribe.transcribe_batch(batch, config, conn)

    assert message.transcript == ""
    assert "model not installed" in message.transcript_error
    assert any("1 voice note" in w for w in batch.warnings)


def test_transcribe_disabled_by_config_does_nothing(
    config: Config, conn, tmp_path: Path, monkeypatch
) -> None:
    message = make_voice_message(tmp_path)
    batch = Batch(batch_id="b1", messages=[message])
    calls = []
    monkeypatch.setattr(transcribe, "transcribe_file", lambda path, **kw: calls.append(1))

    transcribe.transcribe_batch(batch, config, conn)  # transcribe_enabled=False by fixture

    assert calls == []
    assert message.transcript == ""


def test_read_message_dir_picks_up_a_persisted_transcript(tmp_path: Path) -> None:
    directory = tmp_path / "msg-1"
    directory.mkdir()
    (directory / "message.json").write_text(
        json.dumps(
            {
                "id": "voice-1",
                "timestamp": 1_700_000_000,
                "text": "",
                "media": {"filename": "voice.ogg", "mimetype": "audio/ogg"},
                "transcript": "already transcribed",
            }
        ),
        encoding="utf-8",
    )
    (directory / "voice.ogg").write_bytes(b"fake-ogg")

    message = collect.read_message_dir(directory, seq=1)

    assert message.transcript == "already transcribed"
    assert message.media is not None and message.media.is_audio
