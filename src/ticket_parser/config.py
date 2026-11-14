"""Configuration, loaded once from .env / the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass(frozen=True)
class Config:
    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str
    imap_folder: str
    allowed_senders: tuple[str, ...]
    mail_enabled: bool

    quiet_period_seconds: int
    drop_dir: Path
    watch_interval_seconds: int

    data_dir: Path
    out_dir: Path
    poll_interval_seconds: int
    retention_days: int
    backlog_warn_threshold: int
    log_level: str
    log_file: Path | None

    llm_enabled: bool
    claude_bin: str
    claude_model: str  # fallback for both passes
    claude_model_cluster: str
    claude_model_investigate: str
    claude_timeout_seconds: int
    claude_concurrency: int
    repo_path: Path | None

    transcribe_enabled: bool
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str

    # Where to look for a document the extension could not include (it was
    # not downloaded in WhatsApp in that tab). The drop folder is skipped.
    document_dirs: tuple[Path, ...] = ()

    extra: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.sqlite3"

    def require_imap(self) -> None:
        missing = [
            name
            for name, value in (
                ("IMAP_HOST", self.imap_host),
                ("IMAP_USER", self.imap_user),
                ("IMAP_PASSWORD", self.imap_password),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"missing required config: {', '.join(missing)}")
        if not self.allowed_senders:
            raise ConfigError(
                "ALLOWED_SENDERS is empty; refusing to process mail from anyone"
            )

    def require_repo(self) -> Path:
        if self.repo_path is None:
            raise ConfigError("REPO_PATH is not set; the repo pass needs a repository")
        if not self.repo_path.is_dir():
            raise ConfigError(f"REPO_PATH does not exist: {self.repo_path}")
        return self.repo_path


def load_config(env_file: str | os.PathLike[str] | None = None) -> Config:
    load_dotenv(env_file, override=False)

    data_dir = Path(_str("DATA_DIR", "./data")).expanduser().resolve()
    out_raw = _str("OUT_DIR")
    out_dir = Path(out_raw).expanduser().resolve() if out_raw else data_dir / "out"

    log_raw = _str("LOG_FILE", "./data/ticket-parser.log")
    repo_raw = _str("REPO_PATH")

    senders = tuple(
        s.strip().lower() for s in _str("ALLOWED_SENDERS").split(",") if s.strip()
    )

    return Config(
        imap_host=_str("IMAP_HOST", "imap.gmx.com"),
        imap_port=_int("IMAP_PORT", 993),
        imap_user=_str("IMAP_USER"),
        imap_password=os.getenv("IMAP_PASSWORD", ""),
        imap_folder=_str("IMAP_FOLDER", "INBOX"),
        allowed_senders=senders,
        mail_enabled=_bool("MAIL_ENABLED", False),
        quiet_period_seconds=_int("QUIET_PERIOD_SECONDS", 7200),
        drop_dir=Path(_str("DROP_DIR", "~/Downloads/ticket-parser"))
        .expanduser()
        .resolve(),
        watch_interval_seconds=_int("WATCH_INTERVAL_SECONDS", 30),
        data_dir=data_dir,
        out_dir=out_dir,
        poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 300),
        retention_days=_int("RETENTION_DAYS", 30),
        backlog_warn_threshold=_int("BACKLOG_WARN_THRESHOLD", 50),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        log_file=Path(log_raw).expanduser().resolve() if log_raw else None,
        llm_enabled=_bool("LLM_ENABLED", True),
        claude_bin=_str("CLAUDE_BIN", "claude"),
        claude_model=_str("CLAUDE_MODEL"),
        # The two passes are different jobs: clustering reads a handful of
        # messages, investigation navigates an unfamiliar codebase. Each falls
        # back to CLAUDE_MODEL, which falls back to the CLI's own default.
        claude_model_cluster=_str("CLAUDE_MODEL_CLUSTER") or _str("CLAUDE_MODEL"),
        claude_model_investigate=_str("CLAUDE_MODEL_INVESTIGATE")
        or _str("CLAUDE_MODEL"),
        claude_timeout_seconds=_int("CLAUDE_TIMEOUT_SECONDS", 600),
        claude_concurrency=max(1, _int("CLAUDE_CONCURRENCY", 2)),
        repo_path=Path(repo_raw).expanduser().resolve() if repo_raw else None,
        document_dirs=tuple(
            Path(part.strip()).expanduser()
            for part in _str("DOCUMENT_SEARCH_DIRS", "~/Downloads").split(os.pathsep)
            if part.strip()
        ),
        # faster-whisper: free, offline, no API key. "base" is the sweet spot
        # for a voice note on CPU; int8 keeps it fast without a GPU.
        transcribe_enabled=_bool("TRANSCRIBE_ENABLED", True),
        whisper_model=_str("WHISPER_MODEL", "base"),
        whisper_device=_str("WHISPER_DEVICE", "cpu"),
        whisper_compute_type=_str("WHISPER_COMPUTE_TYPE", "int8"),
    )
