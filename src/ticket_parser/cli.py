"""Command line entry point. Exits non-zero on failure so cron surfaces it."""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import collect, db, mailbox, pipeline, review
from .config import Config, ConfigError, load_config
from .logging_setup import setup_logging
from .storage import Storage

log = logging.getLogger("ticket_parser")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticket-parser",
        description="WhatsApp -> categorised PDF bug intake pipeline",
    )
    parser.add_argument("--env-file", default=None, help="path to .env (default: ./.env)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run every stage but write no PDF and commit nothing",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="no progress output; structured logs only (for cron/systemd)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser(
        "run", help="close a batch if the chat has gone quiet, and render it"
    )
    run.add_argument(
        "--force",
        action="store_true",
        help="close the batch now, without waiting for the quiet period",
    )
    # Accept --dry-run after the subcommand too; SUPPRESS keeps it from
    # clobbering the global flag when it is not given here.
    run.add_argument(
        "--dry-run", action="store_true", default=argparse.SUPPRESS,
        help="run every stage but write no PDF and commit nothing",
    )
    run.add_argument(
        "--html",
        metavar="PATH",
        help="also write the intermediate HTML here, for restyling the template",
    )

    flush = sub.add_parser(
        "flush", help="alias for `run --force`; reviews issues when interactive"
    )
    for target in (run, flush):
        target.add_argument(
            "--review",
            action="store_true",
            help="keep/discard each issue before it is investigated",
        )
        target.add_argument(
            "--no-review",
            action="store_true",
            help="skip triage even on a terminal",
        )

    imp = sub.add_parser(
        "import", help="pull messages exported by the Firefox extension into inbox/"
    )
    imp.add_argument(
        "--from",
        dest="drop_dir",
        default=None,
        metavar="PATH",
        help="drop directory (default: DROP_DIR, or ~/Downloads/ticket-parser)",
    )
    imp.add_argument(
        "--then-run",
        action="store_true",
        help="close the batch and render a report immediately afterwards",
    )
    watch = sub.add_parser(
        "watch",
        help="poll the extension's drop folder and render reports as they close",
    )
    watch.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help="stop after N passes (0 = forever); used by tests",
    )

    mail = sub.add_parser(
        "mail", help="fetch bundles attached to mail in the configured mailbox"
    )
    mail.add_argument(
        "--then-run",
        action="store_true",
        help="import and render immediately afterwards",
    )

    sub.add_parser("status", help="show what is pending and what is ready")
    sub.add_parser("prune", help="drop done/ dirs older than RETENTION_DAYS")

    return parser


def _context(args: argparse.Namespace) -> tuple[Config, Storage]:
    config = load_config(args.env_file)
    setup_logging(config.log_level, config.log_file)
    storage = Storage(config.data_dir, config.out_dir)
    storage.ensure()
    return config, storage


def _wants_review(args: argparse.Namespace) -> bool:
    """Triage runs when a human is there to do it, unless told otherwise."""
    if getattr(args, "no_review", False) or getattr(args, "quiet", False):
        return False
    if getattr(args, "review", False):
        return True
    # Default on for `flush`, which is the deliberate "render this now" command.
    return args.command == "flush" and review.is_interactive()


def cmd_run(args: argparse.Namespace) -> int:
    config, storage = _context(args)
    conn = db.connect(config.db_path)

    result = pipeline.run(
        config,
        storage,
        conn,
        force=getattr(args, "force", False),
        dry_run=args.dry_run,
        show_progress=not getattr(args, "quiet", False),
        interactive_review=_wants_review(args),
    )

    if not result.produced:
        print("nothing to do")
        return 0

    if result.batch is not None and not result.batch.issues:
        print("all issues discarded — no report written")
        return 0

    if getattr(args, "html", None):
        from pathlib import Path

        Path(args.html).write_text(result.html, encoding="utf-8")
        print(f"html: {args.html}")

    batch = result.batch
    assert batch is not None
    print(f"batch {batch.batch_id}: {len(batch.messages)} messages, {len(batch.issues)} issues")
    for index, issue in enumerate(batch.issues, start=1):
        verdict = f" [{issue.verdict.label}]" if issue.verdict.ran else ""
        print(f"  {index}. {issue.title} ({issue.category}/{issue.severity}){verdict}")
    for warning in batch.warnings:
        print(f"  ! {warning}")
    if result.pdf_path:
        print(f"pdf: {result.pdf_path}")
    return 0


def cmd_flush(args: argparse.Namespace) -> int:
    args.force = True
    return cmd_run(args)


def cmd_import(args: argparse.Namespace) -> int:
    from pathlib import Path

    config, storage = _context(args)
    drop_dir = Path(args.drop_dir).expanduser() if args.drop_dir else config.drop_dir

    imported, skipped = collect.import_drop(storage, drop_dir)
    print(f"imported {imported} message(s) from {drop_dir}")
    if skipped:
        print(f"  {skipped} already present, left alone")
    if not imported:
        print("nothing new — export from the extension first")
        return 0

    if args.then_run:
        args.force = True
        args.html = None
        return cmd_run(args)

    print("now run: ticket-parser flush")
    return 0


def watch_tick(
    config: Config,
    storage: Storage,
    conn,
    *,
    dry_run: bool = False,
    show_progress: bool = False,
) -> pipeline.PipelineResult:
    """One pass: import anything the extension dropped, then try to render.

    Split out from the loop so it can be tested without sleeping. The extension
    writes message.json *last*, so a directory containing it is complete -- a
    download still in flight is skipped and picked up on the next tick.
    """
    if config.mail_enabled:
        try:
            report = mailbox.poll(config, conn, dry_run=dry_run)
            if report.bundles:
                log.info("bundles from mail", extra={"files": report.bundles})
        except Exception as exc:
            # A mailbox that is down must not stop the local drop folder from
            # being processed.
            log.error("mailbox poll failed", extra={"error": str(exc)})

    if config.drop_dir.is_dir():
        imported, _ = collect.import_drop(storage, config.drop_dir)
        if imported:
            log.info("imported from drop", extra={"count": imported})

    return pipeline.run(
        config, storage, conn, dry_run=dry_run, show_progress=show_progress
    )


def cmd_watch(args: argparse.Namespace) -> int:
    config, storage = _context(args)
    conn = db.connect(config.db_path)
    interval = max(5, config.watch_interval_seconds)

    log.info(
        "watching for exports",
        extra={"drop_dir": str(config.drop_dir), "interval": interval},
    )
    print(f"watching {config.drop_dir} every {interval}s — Ctrl-C to stop")

    iterations = 0
    try:
        while True:
            try:
                result = watch_tick(
                    config,
                    storage,
                    conn,
                    dry_run=args.dry_run,
                    show_progress=not args.quiet,
                )
                if result.produced and result.pdf_path:
                    print(f"report: {result.pdf_path}")
            except Exception as exc:
                # A watcher that dies on one bad batch is worse than one that
                # complains and keeps going.
                log.error("watch tick failed", extra={"error": str(exc)})

            iterations += 1
            if args.max_iterations and iterations >= args.max_iterations:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("watch stopped")
        return 0


def cmd_mail(args: argparse.Namespace) -> int:
    config, storage = _context(args)
    conn = db.connect(config.db_path)

    if not config.mail_enabled:
        print("MAIL_ENABLED is false; nothing to do")
        return 0

    report = mailbox.poll(config, conn, dry_run=args.dry_run)
    print(
        f"scanned {report.scanned} message(s): {len(report.bundles)} bundle(s), "
        f"{len(report.duplicates)} already seen, {len(report.rejected)} not allowlisted"
    )
    for name in report.bundles:
        print(f"  {name}")
    for error in report.errors:
        print(f"  ! {error}")

    if report.bundles and args.then_run:
        args.force = True
        args.html = None
        collect.import_drop(storage, config.drop_dir)
        return cmd_run(args)

    return 1 if report.errors else 0


def cmd_status(args: argparse.Namespace) -> int:
    config, storage = _context(args)
    conn = db.connect(config.db_path)

    directories = collect.pending_dirs(storage)
    messages = []
    for index, directory in enumerate(directories, start=1):
        try:
            messages.append(collect.read_message_dir(directory, index))
        except collect.CollectError:
            continue

    ready = collect.batch_is_ready(messages, config.quiet_period_seconds)
    seen = conn.execute("SELECT COUNT(*) AS n FROM seen_chat_messages").fetchone()["n"]

    print(f"pending messages: {len(directories)}")
    print(f"batch ready:      {'yes' if ready else 'no'} (quiet period {config.quiet_period_seconds}s)")
    if messages and not ready:
        newest = max(m.timestamp for m in messages)
        remaining = int(config.quiet_period_seconds - (time.time() - newest))
        print(f"closes in:        ~{max(0, remaining)}s of silence")
    print(f"triaged to date:  {seen} messages")

    if len(directories) > config.backlog_warn_threshold:
        print(
            f"WARNING: {len(directories)} unprocessed messages "
            f"(threshold {config.backlog_warn_threshold}) -- the pipeline has been failing"
        )
        return 1
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    config, storage = _context(args)
    removed = storage.prune_done(config.retention_days)
    log.info("prune complete", extra={"removed": len(removed)})
    print(f"pruned {len(removed)} committed batches")
    return 0


COMMANDS = {
    "run": cmd_run,
    "flush": cmd_flush,
    "import": cmd_import,
    "watch": cmd_watch,
    "mail": cmd_mail,
    "status": cmd_status,
    "prune": cmd_prune,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        logging.getLogger("ticket_parser").exception("fatal", extra={"error": str(exc)})
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
