"""Confront each issue with the repository, via a read-only `claude` session.

One invocation per issue, `cwd` pinned to REPO_PATH, with an allowlist that can
read the tree and its git history and nothing else. Results are cached on
(issue content, repo HEAD, prompt version), so re-rendering a PDF never re-burns
a CLI call, and a new commit invalidates the verdict rather than serving a stale
one.
"""

from __future__ import annotations

import json
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from sqlite3 import Connection
from typing import Callable

from . import db
from .claude_cli import REPO_READONLY_TOOLS, ClaudeError, ClaudeRunner, extract_json
from .models import Batch, ChatMessage, Evidence, Issue, RepoVerdict

log = logging.getLogger(__name__)

PROMPT_VERSION = "investigate-v1"

_PROMPT = """A non-technical product owner reported the following issue over \
WhatsApp. You are in the repository for the product it concerns.

Issue: {title}
Category: {category}
Reported severity: {severity}

What they said, verbatim (a line marked "voice transcript" is an automated \
transcription of a voice note and may contain transcription errors):
{quotes}

Investigate whether this problem plausibly exists in this codebase as \
described. Read the relevant code and use git history where it helps. Do not \
modify anything.

Then reply with JSON only. No prose outside the JSON, no code fence:

{{
  "verdict": "confirmed" | "not_found" | "inconclusive",
  "confidence": "low" | "medium" | "high",
  "observations": "2-4 sentences: what the code does, and why that does or \
does not match the report. Name the files you relied on.",
  "suggested_fix": "2-3 sentences describing the change you would make, naming \
the files and functions. Empty string if the verdict is not 'confirmed'.",
  "evidence": [{{"file": "relative/path.py", "line": 42, "note": "what this shows"}}]
}}

Be honest about uncertainty. "inconclusive" is a better answer than a confident \
guess: this text goes into a document a human will act on. If the reported \
behaviour is not something this repository could produce, say "not_found" and \
explain why in observations.
"""


def repo_head(repo_path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def _quote_body(message: ChatMessage) -> str:
    if message.text:
        return message.text
    if message.transcript:
        return f"(voice transcript) {message.transcript}"
    return ""


def build_prompt(issue: Issue) -> str:
    quotes = "\n".join(
        f"  [{message.display_time}] {message.author_name}: {_quote_body(message)}"
        + (f"  (screenshot: {message.media.filename})" if message.media else "")
        for message in issue.messages
    )
    return _PROMPT.format(
        title=issue.title,
        category=issue.category,
        severity=issue.severity,
        quotes=quotes,
    )


def cache_key(issue: Issue, head: str, model: str = "") -> str:
    """Identity of a verdict.

    The model is part of the key: a Haiku verdict and an Opus verdict are not
    interchangeable, so switching models must re-investigate rather than serve
    the other model's answer.
    """
    import hashlib

    payload = "\x1f".join(
        (issue.content_hash(), head, PROMPT_VERSION, model or "cli-default")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _verdict_from_payload(payload: object, head: str) -> RepoVerdict:
    if not isinstance(payload, dict):
        return RepoVerdict(
            verdict="inconclusive",
            repo_head=head,
            error="model reply was not a JSON object",
        )
    verdict = str(payload.get("verdict", "inconclusive")).strip().lower()
    if verdict not in {"confirmed", "not_found", "inconclusive"}:
        verdict = "inconclusive"

    evidence: list[Evidence] = []
    for raw in payload.get("evidence") or []:
        if not isinstance(raw, dict) or not raw.get("file"):
            continue
        line = raw.get("line")
        try:
            line_number = int(line) if line is not None else None
        except (TypeError, ValueError):
            line_number = None
        evidence.append(
            Evidence(
                file=str(raw["file"]),
                line=line_number,
                note=str(raw.get("note") or ""),
            )
        )

    confidence = str(payload.get("confidence", "")).strip().lower()
    return RepoVerdict(
        verdict=verdict,
        confidence=confidence if confidence in {"low", "medium", "high"} else "",
        observations=str(payload.get("observations") or "").strip(),
        suggested_fix=str(payload.get("suggested_fix") or "").strip(),
        evidence=evidence,
        repo_head=head,
    )


def _serialise(verdict: RepoVerdict) -> str:
    return json.dumps(
        {
            "verdict": verdict.verdict,
            "confidence": verdict.confidence,
            "observations": verdict.observations,
            "suggested_fix": verdict.suggested_fix,
            "evidence": [
                {"file": e.file, "line": e.line, "note": e.note} for e in verdict.evidence
            ],
            "repo_head": verdict.repo_head,
        },
        ensure_ascii=False,
    )


def investigate_issue(
    issue: Issue,
    runner: ClaudeRunner,
    repo_path: Path,
    head: str,
    *,
    timeout: int | None = None,
    model: str = "",
) -> RepoVerdict:
    try:
        result = runner.run(
            build_prompt(issue),
            cwd=repo_path,
            allowed_tools=REPO_READONLY_TOOLS,
            timeout=timeout,
            label=f"investigate:{issue.key}",
            model=model,
        )
        return _verdict_from_payload(extract_json(result.text), head)
    except ClaudeError as exc:
        # A failed investigation must never cost us the report.
        log.error(
            "investigation failed",
            extra={"issue": issue.key, "error": str(exc)},
        )
        return RepoVerdict(verdict="not_analysed", repo_head=head, error=str(exc))


def investigate_batch(
    batch: Batch,
    runner: ClaudeRunner | None,
    conn: Connection,
    repo_path: Path | None,
    *,
    enabled: bool = True,
    concurrency: int = 2,
    timeout: int | None = None,
    model: str = "",
    on_result: Callable[[Issue, int, int], None] | None = None,
    on_start: Callable[[int], None] | None = None,
) -> Batch:
    if not enabled or runner is None or repo_path is None:
        batch.warnings.append("Repository analysis is disabled; pages carry no verdict.")
        return batch

    head = repo_head(repo_path)
    pending: list[Issue] = []

    for issue in batch.issues:
        key = cache_key(issue, head, model)
        cached = db.cache_get(conn, key)
        if cached:
            issue.verdict = _verdict_from_payload(json.loads(cached), head)
            log.info("verdict served from cache", extra={"issue": issue.key})
        else:
            pending.append(issue)

    if on_start is not None:
        on_start(len(pending))

    if pending:
        completed = 0
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = {
                pool.submit(
                    investigate_issue,
                    issue,
                    runner,
                    repo_path,
                    head,
                    timeout=timeout,
                    model=model,
                ): issue
                for issue in pending
            }
            # as_completed rather than map: a verdict should be reported the
            # moment it lands, not once the slowest one finishes.
            for future in as_completed(futures):
                issue = futures[future]
                issue.verdict = future.result()
                completed += 1
                if on_result is not None:
                    on_result(issue, completed, len(pending))

        for issue in pending:
            verdict = issue.verdict
            if verdict.verdict == "not_analysed":
                batch.warnings.append(
                    f"Repository analysis failed for {issue.key}: {verdict.error}"
                )
                continue  # never cache a failure
            with db.transaction(conn):
                db.cache_put(
                    conn,
                    cache_key=cache_key(issue, head, model),
                    message_id=batch.batch_id,
                    issue_key=issue.key,
                    repo_head=head,
                    prompt_version=PROMPT_VERSION,
                    payload_json=_serialise(verdict),
                )

    return batch
