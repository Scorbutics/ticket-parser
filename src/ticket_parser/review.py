"""Human triage between clustering and investigation.

Not everything a product owner writes is a bug -- plenty of it is questions,
context, or chatter that the clusterer will happily label as an issue. Each
investigation is a full `claude` session against the repository, so discarding
those first saves both money and a page of noise in the report.

Runs only on an interactive terminal. Anything non-interactive (cron, systemd,
a pipe) keeps every issue, because a prompt nobody can answer would hang.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from .models import Batch, Issue

MAX_PREVIEW_CHARS = 400

HELP = """
  y / enter  keep this issue (it is a real bug worth investigating)
  n          discard it (a question, chatter, or a duplicate)
  a          keep this and all remaining issues
  x          discard this and all remaining issues
  ?          show this help
""".rstrip()


@dataclass
class ReviewOutcome:
    kept: list[Issue]
    discarded: list[Issue]

    @property
    def reviewed(self) -> bool:
        return bool(self.kept or self.discarded)


def is_interactive(stdin=None, stdout=None) -> bool:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        return bool(stdin.isatty() and stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _preview(issue: Issue) -> list[str]:
    lines = []
    for message in issue.messages:
        stamp = message.sent_at.strftime("%d/%m %H:%M")
        text = message.text.strip()
        if len(text) > MAX_PREVIEW_CHARS:
            text = text[:MAX_PREVIEW_CHARS].rstrip() + "…"
        marker = ""
        if message.media:
            marker = " [screenshot]"
        elif message.document_name:
            marker = f" [{message.document_name}]"
        elif message.media_error:
            marker = " [attachment missing]"
        body = text or "(no text)"
        # Indent continuation lines so multi-line messages stay readable.
        first, *rest = body.split("\n")
        lines.append(f"  [{stamp}] {message.author_name}: {first}{marker if not rest else ''}")
        for extra in rest:
            lines.append(f"      {extra}")
        if rest and marker:
            lines.append(f"      {marker.strip()}")
    return lines


def review_issues(batch: Batch, *, stdin=None, stdout=None) -> ReviewOutcome:
    """Walk the issues, keeping or discarding each. Mutates `batch.issues`."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    kept: list[Issue] = []
    discarded: list[Issue] = []
    total = len(batch.issues)
    decide_rest: str | None = None

    stdout.write(
        f"\n{total} issue(s) found. Keep the real bugs, discard questions and "
        "chatter.\nOnly kept issues are investigated against the repository.\n"
    )
    stdout.flush()

    for index, issue in enumerate(batch.issues, start=1):
        if decide_rest == "keep":
            kept.append(issue)
            continue
        if decide_rest == "discard":
            discarded.append(issue)
            continue

        stdout.write("\n" + "─" * 64 + "\n")
        stdout.write(
            f"Issue {index}/{total} · {issue.category} · severity {issue.severity}\n"
            f"{issue.title}\n\n"
        )
        stdout.write("\n".join(_preview(issue)) + "\n\n")
        stdout.flush()

        while True:
            stdout.write("Keep? [Y/n/a=keep all/x=discard all/?] ")
            stdout.flush()
            try:
                answer = stdin.readline()
            except (KeyboardInterrupt, EOFError):
                answer = ""
            if answer == "":  # EOF: the terminal went away, keep what is left
                kept.append(issue)
                decide_rest = "keep"
                stdout.write("\ninput closed — keeping the remaining issues\n")
                break

            choice = answer.strip().lower()
            if choice in {"", "y", "yes", "k"}:
                kept.append(issue)
                break
            if choice in {"n", "no", "d"}:
                discarded.append(issue)
                break
            if choice == "a":
                kept.append(issue)
                decide_rest = "keep"
                break
            if choice == "x":
                discarded.append(issue)
                decide_rest = "discard"
                break
            if choice in {"?", "h", "help"}:
                stdout.write(HELP + "\n")
                continue
            stdout.write(f"  unrecognised: {choice!r}\n")

    batch.issues = kept
    for position, issue in enumerate(kept, start=1):
        issue.key = f"issue-{position}"

    if discarded:
        titles = "; ".join(issue.title for issue in discarded)
        batch.warnings.append(
            f"{len(discarded)} issue(s) discarded during review: {titles}. Their "
            "messages are marked as triaged and will not reappear."
        )

    stdout.write(
        f"\nkeeping {len(kept)} issue(s), discarding {len(discarded)}.\n"
    )
    stdout.flush()
    return ReviewOutcome(kept=kept, discarded=discarded)
