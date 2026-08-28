"""Live feedback for the slow stages.

A `claude` call can take minutes, and JSON logs only appear once it returns. A
run that prints nothing for 93 seconds is indistinguishable from a hung one, so
long stages get a ticking status line.

Written to stderr so stdout stays clean for the structured log. On a terminal it
rewrites one line; when piped (cron, systemd) it emits a periodic line instead
of thousands of carriage returns.
"""

from __future__ import annotations

import itertools
import sys
import threading
import time
from types import TracebackType

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PIPE_INTERVAL_SECONDS = 30


class Progress:
    """A ticking status line. Silent when not wanted."""

    def __init__(self, label: str, *, enabled: bool = True, stream=None) -> None:
        self.label = label
        self.stream = stream or sys.stderr
        self.enabled = enabled and not self.stream.closed
        self.is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0
        self._suffix = ""

    def update(self, suffix: str) -> None:
        """Add detail to the line, e.g. '3/6 done'."""
        self._suffix = suffix

    def _render(self, frame: str, elapsed: float) -> str:
        parts = [self.label]
        if self._suffix:
            parts.append(self._suffix)
        return f"{frame} {' — '.join(parts)} ({elapsed:.0f}s)"

    def _run(self) -> None:
        frames = itertools.cycle(SPINNER)
        last_pipe_write = 0.0
        while not self._stop.is_set():
            elapsed = time.monotonic() - self._started
            if self.is_tty:
                line = self._render(next(frames), elapsed)
                self.stream.write(f"\r\033[K{line}")
                self.stream.flush()
                self._stop.wait(0.12)
            else:
                if elapsed - last_pipe_write >= PIPE_INTERVAL_SECONDS:
                    last_pipe_write = elapsed
                    self.stream.write(self._render("...", elapsed) + "\n")
                    self.stream.flush()
                self._stop.wait(1.0)

    def __enter__(self) -> "Progress":
        if not self.enabled:
            return self
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        elapsed = time.monotonic() - self._started
        mark = "✗" if exc_type else "✓"
        if self.is_tty:
            self.stream.write("\r\033[K")
        self.stream.write(f"{mark} {self.label} ({elapsed:.0f}s)\n")
        self.stream.flush()


def note(message: str, *, enabled: bool = True, stream=None) -> None:
    """A one-off line alongside the progress output."""
    if not enabled:
        return
    target = stream or sys.stderr
    target.write(f"  {message}\n")
    target.flush()
