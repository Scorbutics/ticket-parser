"""Human triage: not everything the product owner writes is a bug."""

from __future__ import annotations

import io

from ticket_parser import review
from ticket_parser.models import Batch, ChatMessage, Issue


def make_batch(*titles: str) -> Batch:
    messages = []
    issues = []
    for index, title in enumerate(titles, start=1):
        message = ChatMessage(
            seq=index,
            wa_id=f"msg{index}",
            chat_id="chat",
            author_name="Emmanuel DE",
            timestamp=1_700_000_000 + index * 60,
            text=f"texte pour {title}",
        )
        messages.append(message)
        issues.append(
            Issue(
                key=f"issue-{index}",
                title=title,
                category="auth",
                severity="high",
                messages=[message],
            )
        )
    return Batch(batch_id="test", messages=messages, issues=issues)


def run_review(batch: Batch, keystrokes: str) -> tuple[review.ReviewOutcome, str]:
    out = io.StringIO()
    outcome = review.review_issues(batch, stdin=io.StringIO(keystrokes), stdout=out)
    return outcome, out.getvalue()


def test_discarded_issues_are_removed(monkeypatch) -> None:
    batch = make_batch("Login fails", "Is the deploy done?", "Cart total wrong")

    outcome, _ = run_review(batch, "y\nn\ny\n")

    assert [i.title for i in batch.issues] == ["Login fails", "Cart total wrong"]
    assert [i.title for i in outcome.discarded] == ["Is the deploy done?"]


def test_enter_keeps_the_issue() -> None:
    batch = make_batch("Login fails")

    run_review(batch, "\n")

    assert len(batch.issues) == 1


def test_keys_are_renumbered_after_discarding() -> None:
    batch = make_batch("A", "B", "C")

    run_review(batch, "n\ny\ny\n")

    assert [i.key for i in batch.issues] == ["issue-1", "issue-2"]
    assert [i.title for i in batch.issues] == ["B", "C"]


def test_a_keeps_everything_remaining() -> None:
    batch = make_batch("A", "B", "C", "D")

    run_review(batch, "n\na\n")

    assert [i.title for i in batch.issues] == ["B", "C", "D"]


def test_x_discards_everything_remaining() -> None:
    batch = make_batch("A", "B", "C")

    run_review(batch, "y\nx\n")

    assert [i.title for i in batch.issues] == ["A"]


def test_discarding_everything_leaves_no_issues() -> None:
    batch = make_batch("A", "B")

    run_review(batch, "x\n")

    assert batch.issues == []


def test_the_discard_is_recorded_on_the_report() -> None:
    batch = make_batch("Login fails", "Is the deploy done?")

    run_review(batch, "y\nn\n")

    assert any("1 issue(s) discarded" in w for w in batch.warnings)
    assert any("Is the deploy done?" in w for w in batch.warnings)


def test_messages_stay_on_the_batch_so_they_are_marked_triaged() -> None:
    """Discarded questions must not come back in the next report."""
    batch = make_batch("Login fails", "Is the deploy done?")

    run_review(batch, "y\nn\n")

    assert len(batch.messages) == 2  # both still committed as seen


def test_the_prompt_shows_the_messages_behind_each_issue() -> None:
    batch = make_batch("Login fails")

    _, output = run_review(batch, "y\n")

    assert "texte pour Login fails" in output
    assert "Emmanuel DE" in output
    assert "auth" in output


def test_unrecognised_input_reprompts() -> None:
    batch = make_batch("A")

    _, output = run_review(batch, "wat\ny\n")

    assert "unrecognised" in output
    assert len(batch.issues) == 1


def test_help_is_available() -> None:
    batch = make_batch("A")

    _, output = run_review(batch, "?\ny\n")

    assert "keep this and all remaining" in output


def test_closed_input_keeps_everything() -> None:
    """A terminal that goes away must not silently discard the report."""
    batch = make_batch("A", "B")

    run_review(batch, "")

    assert len(batch.issues) == 2


def test_not_interactive_when_piped() -> None:
    assert review.is_interactive(io.StringIO(), io.StringIO()) is False
