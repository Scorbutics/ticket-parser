"""`watch` polls the extension's drop folder; no daemon, no Node, no browser."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ticket_parser import db
from ticket_parser.cli import watch_tick
from ticket_parser.config import Config
from ticket_parser.storage import Storage

from test_import_drop import write_extension_export


@pytest.fixture
def conn(config: Config):
    return db.connect(config.db_path)


def test_tick_imports_and_renders_in_one_pass(
    config: Config, storage: Storage, conn, monkeypatch
) -> None:
    rendered = {}

    def fake_pdf(html: str, path: Path) -> Path:
        rendered["path"] = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-fake")
        return path

    monkeypatch.setattr("ticket_parser.render.html_to_pdf", fake_pdf)
    object.__setattr__(config, "llm_enabled", False)

    write_extension_export(
        config.drop_dir,
        message_id="false_x_A",
        text="le login est cassé",
        timestamp=int(time.time() - 9000),
    )

    result = watch_tick(config, storage, conn)

    assert result.produced
    assert result.pdf_path == rendered["path"]
    assert list(config.drop_dir.iterdir()) == []  # consumed


def test_tick_is_a_noop_when_nothing_was_exported(
    config: Config, storage: Storage, conn
) -> None:
    config.drop_dir.mkdir(parents=True, exist_ok=True)

    result = watch_tick(config, storage, conn)

    assert not result.produced


def test_tick_survives_a_missing_drop_dir(
    config: Config, storage: Storage, conn
) -> None:
    assert not config.drop_dir.exists()

    result = watch_tick(config, storage, conn)  # must not raise

    assert not result.produced


def test_a_download_still_in_flight_is_left_for_the_next_tick(
    config: Config, storage: Storage, conn
) -> None:
    """The extension writes message.json last, so its absence means incomplete."""
    partial = config.drop_dir / "false_x_B"
    partial.mkdir(parents=True)
    (partial / "false_x_B.png").write_bytes(b"\x89PNG")

    watch_tick(config, storage, conn)

    assert (partial / "false_x_B.png").exists()  # untouched, not half-imported
    assert list(storage.inbox.iterdir()) == []


def test_tick_holds_the_batch_while_the_chat_is_still_active(
    config: Config, storage: Storage, conn
) -> None:
    write_extension_export(
        config.drop_dir,
        message_id="false_x_A",
        text="just now",
        timestamp=int(time.time()),
    )

    result = watch_tick(config, storage, conn)

    assert not result.produced  # imported, but the quiet period has not elapsed
    assert (storage.inbox / "false_x_A").is_dir()
