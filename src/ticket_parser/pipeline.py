"""collect -> cluster -> investigate -> render, with the failure policy.

Policy, decided up front so it is not re-litigated per stage: an LLM failure
never costs you the document. Clustering falls back to one flat issue, the repo
pass falls back to a page with no verdict, and both leave a banner on the cover
page saying what broke. The only hard failure is an unwritable PDF.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection

from . import collect, enrich, investigate, render, review, transcribe
from .claude_cli import ClaudeRunner, SubprocessClaudeRunner
from .config import Config
from .models import Batch
from .progress import Progress, note
from .storage import Storage

log = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    batch: Batch | None
    pdf_path: Path | None
    html: str = ""

    @property
    def produced(self) -> bool:
        return self.batch is not None


def build_runner(config: Config) -> ClaudeRunner | None:
    if not config.llm_enabled:
        log.info("LLM passes disabled by config")
        return None
    runner = SubprocessClaudeRunner(
        config.claude_bin,
        model=config.claude_model,
        default_timeout=config.claude_timeout_seconds,
    )
    if not runner.available():
        log.error(
            "claude CLI not found; continuing without LLM passes",
            extra={"binary": config.claude_bin},
        )
        return None
    return runner


def run(
    config: Config,
    storage: Storage,
    conn: Connection,
    *,
    runner: ClaudeRunner | None = None,
    force: bool = False,
    dry_run: bool = False,
    now: float | None = None,
    show_progress: bool = False,
    interactive_review: bool = False,
) -> PipelineResult:
    batch = collect.collect_batch(
        storage,
        conn,
        quiet_period_seconds=config.quiet_period_seconds,
        force=force,
        now=now,
    )
    if batch is None:
        return PipelineResult(batch=None, pdf_path=None)

    log.info(
        "batch collected",
        extra={"batch": batch.batch_id, "messages": len(batch.messages)},
    )

    with Progress(
        "transcribing voice notes",
        enabled=show_progress
        and config.transcribe_enabled
        and any(m.media and m.media.is_audio for m in batch.messages),
    ):
        transcribe.transcribe_batch(batch, config, conn)

    if runner is None:
        runner = build_runner(config)
    llm_on = runner is not None
    if config.llm_enabled and not llm_on:
        batch.warnings.append(
            "The `claude` CLI was unavailable; this report has no categorisation "
            "or code analysis."
        )

    with Progress(
        f"grouping {len(batch.messages)} messages into issues"
        + (f" ({config.claude_model_cluster})" if config.claude_model_cluster else ""),
        enabled=show_progress and llm_on,
    ):
        enrich.cluster(
            batch,
            runner,
            enabled=llm_on,
            timeout=config.claude_timeout_seconds,
            model=config.claude_model_cluster,
        )
    log.info("clustered", extra={"batch": batch.batch_id, "issues": len(batch.issues)})

    if show_progress and not interactive_review:
        for index, issue in enumerate(batch.issues, start=1):
            note(f"{index}. {issue.title} ({issue.category}/{issue.severity})")

    # Human triage before any money is spent: an investigation is a full
    # `claude` session, and a question from the product owner is not a bug.
    if interactive_review and batch.issues:
        review.review_issues(batch)
        if not batch.issues:
            log.info("every issue was discarded", extra={"batch": batch.batch_id})
            # Still committed: discarding is a triage decision, so those
            # messages must not come back in the next report.
            if not dry_run:
                collect.commit_batch(storage, conn, batch)
            return PipelineResult(batch=batch, pdf_path=None)

    investigating = Progress(
        "checking issues against the repository", enabled=show_progress and llm_on
    )

    def announce(pending: int) -> None:
        if pending:
            investigating.label = (
                f"checking {pending} issue(s) against the repository"
                + (
                    f" ({config.claude_model_investigate})"
                    if config.claude_model_investigate
                    else ""
                )
            )
            investigating.update(f"0/{pending} done")
        else:
            investigating.enabled = False

    def reported(issue, done: int, total: int) -> None:
        investigating.update(f"{done}/{total} done")
        note(f"{issue.key}: {issue.title} — {issue.verdict.label}", enabled=show_progress)

    with investigating:
        investigate.investigate_batch(
            batch,
            runner,
            conn,
            config.repo_path,
            enabled=llm_on and config.repo_path is not None,
            concurrency=config.claude_concurrency,
            timeout=config.claude_timeout_seconds,
            model=config.claude_model_investigate,
            on_start=announce,
            on_result=reported,
        )

    with Progress("rendering the PDF", enabled=show_progress and not dry_run):
        pdf_path, html = render.render_batch(batch, config.out_dir, dry_run=dry_run)

    if not dry_run:
        collect.commit_batch(storage, conn, batch)
        log.info(
            "batch committed",
            extra={"batch": batch.batch_id, "pdf": str(pdf_path) if pdf_path else None},
        )

    return PipelineResult(batch=batch, pdf_path=pdf_path, html=html)
