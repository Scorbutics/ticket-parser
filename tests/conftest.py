from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ticket_parser.config import Config  # noqa: E402
from ticket_parser.storage import Storage  # noqa: E402


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        imap_host="imap.example.invalid",
        imap_port=993,
        imap_user="me@example.invalid",
        imap_password="secret",
        imap_folder="INBOX",
        allowed_senders=("me@example.invalid",),
        mail_enabled=False,
        quiet_period_seconds=1800,
        drop_dir=tmp_path / "drop",
        watch_interval_seconds=5,
        data_dir=tmp_path / "data",
        out_dir=tmp_path / "data" / "out",
        poll_interval_seconds=300,
        retention_days=30,
        backlog_warn_threshold=50,
        log_level="INFO",
        log_file=None,
        llm_enabled=True,
        claude_bin="claude",
        claude_model="",
        claude_model_cluster="",
        claude_model_investigate="",
        claude_timeout_seconds=60,
        claude_concurrency=2,
        repo_path=None,
        transcribe_enabled=False,
        whisper_model="base",
        whisper_device="cpu",
        whisper_compute_type="int8",
    )


@pytest.fixture
def storage(config: Config) -> Storage:
    store = Storage(config.data_dir, config.out_dir)
    store.ensure()
    return store
