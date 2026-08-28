"""On-disk layout and the atomic moves between its stages.

    data/
      inbox/    raw attachments + .eml, written on arrival, never modified
      staging/  in-progress processing for one message
      done/     committed .eml + originals, pruned after RETENTION_DAYS
      out/      generated PDFs, never auto-pruned

Every transition is an ``os.rename`` within one filesystem, so a crash leaves
either the old state or the new one, never a half-moved directory. A message dir
sitting in ``staging/`` after a crash is intentional: it is inspectable and
replayable.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from pathlib import Path

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug_for(message_id: str) -> str:
    """Filesystem-safe, stable, collision-resistant id for a Message-ID."""
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:16]
    readable = _UNSAFE.sub("-", message_id.strip("<>"))[:48].strip("-")
    return f"{readable}-{digest}" if readable else digest


def safe_filename(name: str, fallback: str = "attachment.bin") -> str:
    """Strip any path component an attachment filename might be carrying."""
    base = os.path.basename(name or "").strip()
    base = base.replace("\x00", "")
    if base in {"", ".", ".."}:
        return fallback
    cleaned = _UNSAFE.sub("_", base)
    return cleaned[:180] or fallback


class Storage:
    def __init__(self, data_dir: Path, out_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.inbox = self.data_dir / "inbox"
        self.staging = self.data_dir / "staging"
        self.done = self.data_dir / "done"
        self.out = Path(out_dir) if out_dir else self.data_dir / "out"

    def ensure(self) -> None:
        for path in (self.inbox, self.staging, self.done, self.out):
            path.mkdir(parents=True, exist_ok=True)

    # -- locations ---------------------------------------------------------

    def inbox_dir(self, slug: str) -> Path:
        return self.inbox / slug

    def staging_dir(self, slug: str) -> Path:
        return self.staging / slug

    def done_dir(self, slug: str) -> Path:
        return self.done / slug

    def find(self, slug: str) -> Path | None:
        for candidate in (self.done / slug, self.staging / slug, self.inbox / slug):
            if candidate.is_dir():
                return candidate
        return None

    # -- writes ------------------------------------------------------------

    def new_incoming(self, slug: str) -> Path:
        """A scratch dir *outside* inbox/, promoted atomically by commit_incoming."""
        pending = self.inbox / f".{slug}.partial"
        if pending.exists():
            shutil.rmtree(pending)
        pending.mkdir(parents=True)
        return pending

    def commit_incoming(self, pending: Path, slug: str) -> Path:
        target = self.inbox_dir(slug)
        if target.exists():
            shutil.rmtree(pending)
            return target
        fsync_tree(pending)
        os.rename(pending, target)
        fsync_dir(self.inbox)
        return target

    def to_staging(self, slug: str) -> Path:
        source, target = self.inbox_dir(slug), self.staging_dir(slug)
        if target.is_dir():
            return target  # crash recovery: already moved
        os.rename(source, target)
        fsync_dir(self.staging)
        return target

    def to_done(self, slug: str) -> Path:
        source, target = self.staging_dir(slug), self.done_dir(slug)
        if target.is_dir():
            return target
        fsync_tree(source)
        os.rename(source, target)
        fsync_dir(self.done)
        return target

    def replay_to_staging(self, slug: str) -> Path:
        """Copy a committed message back into staging for reprocessing."""
        source, target = self.done_dir(slug), self.staging_dir(slug)
        if not source.is_dir():
            raise FileNotFoundError(f"no committed message at {source}")
        if target.is_dir():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        return target

    # -- retention ---------------------------------------------------------

    def prune_done(self, retention_days: int) -> list[str]:
        """Remove committed dirs older than the retention window. out/ is never touched."""
        if retention_days <= 0:
            return []
        cutoff = time.time() - retention_days * 86400
        removed: list[str] = []
        for entry in sorted(self.done.iterdir()) if self.done.is_dir() else []:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
                removed.append(entry.name)
        return removed


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.rename(tmp, path)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_tree(path: Path) -> None:
    """Force every file in a tree to disk before we rename it into place."""
    for entry in sorted(path.rglob("*")):
        if entry.is_file():
            fd = os.open(entry, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    fsync_dir(path)
