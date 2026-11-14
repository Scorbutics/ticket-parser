"""The single seam through which every LLM pass runs.

There is no API key and no HTTP client anywhere in this project: each pass
shells out to the `claude` CLI. Both callers (categorisation, repo
investigation) go through `ClaudeRunner`, so tests inject `FakeClaudeRunner`
and the real binary is never invoked during the test suite.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

log = logging.getLogger(__name__)

# Read-only tool allowlist for the repo pass: inspect the tree and its history,
# never mutate it. Git is included so a verdict can say "introduced in abc123".
REPO_READONLY_TOOLS: tuple[str, ...] = (
    "Read",
    "Grep",
    "Glob",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git blame:*)",
    "Bash(git diff:*)",
    "Bash(git status:*)",
    "Bash(git rev-parse:*)",
)

# Categorisation reasons over text we already hold; it needs no tools at all.
NO_TOOLS: tuple[str, ...] = ()

ALWAYS_DENIED: tuple[str, ...] = ("Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch")


class ClaudeError(RuntimeError):
    """The CLI was missing, timed out, exited non-zero, or returned junk."""


@dataclass
class ClaudeResult:
    text: str
    raw: dict | None = None
    duration_seconds: float = 0.0


class ClaudeRunner(Protocol):
    def run(
        self,
        prompt: str,
        *,
        cwd: Path | None = None,
        allowed_tools: Sequence[str] = NO_TOOLS,
        timeout: int | None = None,
        label: str = "",
        model: str = "",
        extra_dirs: Sequence[Path] = (),
    ) -> ClaudeResult: ...


class SubprocessClaudeRunner:
    def __init__(
        self,
        binary: str = "claude",
        *,
        model: str = "",
        default_timeout: int = 600,
    ) -> None:
        self.binary = binary
        self.model = model
        self.default_timeout = default_timeout

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def _argv(
        self, prompt: str, allowed_tools: Sequence[str], model: str = ""
    ) -> list[str]:
        argv = [self.binary, "-p", prompt, "--output-format", "json"]
        # Per-call model wins: clustering and repo investigation are different
        # jobs and are configured separately.
        effective_model = model or self.model
        if effective_model:
            argv += ["--model", effective_model]
        # An explicit (possibly empty) allowlist means anything absent is denied
        # rather than prompting -- these runs are non-interactive.
        argv += ["--allowed-tools", " ".join(allowed_tools)]
        argv += ["--disallowed-tools", " ".join(ALWAYS_DENIED)]
        return argv

    def run(
        self,
        prompt: str,
        *,
        cwd: Path | None = None,
        allowed_tools: Sequence[str] = NO_TOOLS,
        timeout: int | None = None,
        label: str = "",
        model: str = "",
        extra_dirs: Sequence[Path] = (),
    ) -> ClaudeResult:
        import time

        if not self.available():
            raise ClaudeError(f"`{self.binary}` not found on PATH")

        argv = self._argv(prompt, allowed_tools, model)
        if extra_dirs:
            # Folders outside cwd the read-only tools may open: the batch's
            # screenshots and document figures, so the model can look at them.
            argv += ["--add-dir", *(str(d) for d in extra_dirs)]
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=timeout or self.default_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeError(
                f"claude timed out after {timeout or self.default_timeout}s ({label})"
            ) from exc
        except OSError as exc:
            raise ClaudeError(f"could not run `{self.binary}`: {exc}") from exc

        elapsed = time.monotonic() - started
        if proc.returncode != 0:
            raise ClaudeError(
                f"claude exited {proc.returncode} ({label}): "
                f"{(proc.stderr or '').strip()[:500]}"
            )

        text, raw = _unwrap(proc.stdout)
        log.info(
            "claude call finished",
            extra={
                "label": label,
                "model": model or self.model or "cli-default",
                "seconds": round(elapsed, 2),
                "chars": len(text),
            },
        )
        return ClaudeResult(text=text, raw=raw, duration_seconds=elapsed)


def _unwrap(stdout: str) -> tuple[str, dict | None]:
    """`--output-format json` wraps the answer; tolerate a bare string too."""
    stdout = (stdout or "").strip()
    if not stdout:
        raise ClaudeError("claude produced no output")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout, None
    if isinstance(payload, dict):
        if payload.get("is_error"):
            raise ClaudeError(f"claude reported an error: {payload.get('result')!r}")
        for key in ("result", "text", "content"):
            value = payload.get(key)
            if isinstance(value, str):
                return value.strip(), payload
        return stdout, payload
    return stdout, None


def extract_json(text: str) -> object:
    """Pull the JSON value out of a model reply that may be fenced or chatty."""
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # Take the *outermost* JSON span, so prose wrapping an array does not get
    # truncated to the first object inside it.
    spans = []
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = candidate.find(opener), candidate.rfind(closer)
        if start != -1 and end > start:
            spans.append((start, end))
    for start, end in sorted(spans):
        try:
            return json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            continue
    raise ClaudeError(f"model reply was not JSON: {text[:300]!r}")


@dataclass
class FakeClaudeRunner:
    """Test double. Replies are popped in order; calls are recorded."""

    replies: list[str] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    error: Exception | None = None

    def run(
        self,
        prompt: str,
        *,
        cwd: Path | None = None,
        allowed_tools: Sequence[str] = NO_TOOLS,
        timeout: int | None = None,
        label: str = "",
        model: str = "",
        extra_dirs: Sequence[Path] = (),
    ) -> ClaudeResult:
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": cwd,
                "allowed_tools": tuple(allowed_tools),
                "label": label,
                "model": model,
                "extra_dirs": tuple(extra_dirs),
            }
        )
        if self.error is not None:
            raise self.error
        if not self.replies:
            raise ClaudeError("FakeClaudeRunner ran out of replies")
        return ClaudeResult(text=self.replies.pop(0))
