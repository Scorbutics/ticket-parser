from __future__ import annotations

import json
from pathlib import Path

import pytest

from ticket_parser import db, investigate
from ticket_parser.claude_cli import REPO_READONLY_TOOLS, ClaudeError, FakeClaudeRunner
from ticket_parser.config import Config
from ticket_parser.models import Batch, ChatMessage, Issue

VERDICT = json.dumps(
    {
        "verdict": "confirmed",
        "confidence": "high",
        "observations": "The session cookie is set without a domain in auth/session.py.",
        "suggested_fix": "Set the cookie domain in auth/session.py:88.",
        "evidence": [{"file": "auth/session.py", "line": 88, "note": "cookie set here"}],
    }
)


@pytest.fixture
def conn(config: Config):
    return db.connect(config.db_path)


def make_batch() -> Batch:
    message = ChatMessage(
        seq=1,
        wa_id="msg1",
        chat_id="chat",
        author_name="Product Owner",
        timestamp=1_700_000_000,
        text="login fails on staging",
    )
    issue = Issue(key="issue-1", title="Login fails", category="auth", messages=[message])
    return Batch(batch_id="test", messages=[message], issues=[issue])


def test_build_prompt_includes_a_voice_notes_transcript() -> None:
    message = ChatMessage(
        seq=1,
        wa_id="msg1",
        chat_id="chat",
        author_name="Product Owner",
        timestamp=1_700_000_000,
        text="",
        transcript="the export button crashes the whole app",
    )
    issue = Issue(key="issue-1", title="Export crash", category="ui", messages=[message])

    prompt = investigate.build_prompt(issue)

    assert "the export button crashes the whole app" in prompt
    assert "voice transcript" in prompt


def test_verdict_is_parsed_onto_the_issue(conn, tmp_path: Path) -> None:
    batch = make_batch()
    runner = FakeClaudeRunner(replies=[VERDICT])

    investigate.investigate_batch(batch, runner, conn, tmp_path, concurrency=1)

    verdict = batch.issues[0].verdict
    assert verdict.verdict == "confirmed"
    assert verdict.label == "Found in the code"
    assert verdict.evidence[0].location == "auth/session.py:88"
    assert "cookie" in verdict.suggested_fix


def test_it_runs_in_the_repo_and_cannot_write(conn, tmp_path: Path) -> None:
    batch = make_batch()
    runner = FakeClaudeRunner(replies=[VERDICT])

    investigate.investigate_batch(batch, runner, conn, tmp_path, concurrency=1)

    call = runner.calls[0]
    assert call["cwd"] == tmp_path
    assert call["allowed_tools"] == REPO_READONLY_TOOLS
    assert "Edit" not in call["allowed_tools"]
    assert "Write" not in call["allowed_tools"]


def test_the_prompt_quotes_the_report_verbatim(conn) -> None:
    issue = make_batch().issues[0]
    prompt = investigate.build_prompt(issue)

    assert "login fails on staging" in prompt
    assert "Do not modify anything" in prompt
    assert "inconclusive" in prompt


def test_second_run_is_served_from_cache(conn, tmp_path: Path) -> None:
    runner = FakeClaudeRunner(replies=[VERDICT])
    investigate.investigate_batch(make_batch(), runner, conn, tmp_path, concurrency=1)
    assert len(runner.calls) == 1

    # No replies left: a second CLI call would raise.
    second = make_batch()
    investigate.investigate_batch(second, runner, conn, tmp_path, concurrency=1)

    assert len(runner.calls) == 1
    assert second.issues[0].verdict.verdict == "confirmed"


def test_a_new_repo_head_invalidates_the_cache(conn, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(investigate, "repo_head", lambda path: "aaaa")
    runner = FakeClaudeRunner(replies=[VERDICT, VERDICT])
    investigate.investigate_batch(make_batch(), runner, conn, tmp_path, concurrency=1)

    monkeypatch.setattr(investigate, "repo_head", lambda path: "bbbb")
    investigate.investigate_batch(make_batch(), runner, conn, tmp_path, concurrency=1)

    assert len(runner.calls) == 2


def test_failure_leaves_the_page_intact_and_is_not_cached(conn, tmp_path: Path) -> None:
    batch = make_batch()
    runner = FakeClaudeRunner(error=ClaudeError("timed out"))

    investigate.investigate_batch(batch, runner, conn, tmp_path, concurrency=1)

    assert batch.issues[0].verdict.verdict == "not_analysed"
    assert batch.issues[0].verdict.ran is False
    assert any("Repository analysis failed" in w for w in batch.warnings)
    assert conn.execute("SELECT COUNT(*) AS n FROM analysis_cache").fetchone()["n"] == 0


def test_unknown_verdict_value_falls_back_to_inconclusive(conn, tmp_path: Path) -> None:
    batch = make_batch()
    runner = FakeClaudeRunner(replies=['{"verdict": "definitely broken mate"}'])

    investigate.investigate_batch(batch, runner, conn, tmp_path, concurrency=1)

    assert batch.issues[0].verdict.verdict == "inconclusive"


def test_no_repo_configured_means_no_calls(conn) -> None:
    batch = make_batch()
    runner = FakeClaudeRunner(replies=[VERDICT])

    investigate.investigate_batch(batch, runner, conn, None, concurrency=1)

    assert runner.calls == []
    assert batch.issues[0].verdict.ran is False


def test_each_pass_can_use_a_different_model(conn, tmp_path: Path) -> None:
    from ticket_parser import enrich

    batch = make_batch()
    runner = FakeClaudeRunner(
        replies=['[{"message_ids": [1], "title": "Login fails"}]', VERDICT]
    )

    enrich.cluster(batch, runner, model="claude-haiku-4-5-20251001")
    investigate.investigate_batch(
        batch, runner, conn, tmp_path, concurrency=1, model="claude-opus-5"
    )

    assert runner.calls[0]["label"] == "cluster"
    assert runner.calls[0]["model"] == "claude-haiku-4-5-20251001"
    assert runner.calls[1]["label"].startswith("investigate:")
    assert runner.calls[1]["model"] == "claude-opus-5"


def test_switching_model_invalidates_the_cached_verdict(conn, tmp_path: Path) -> None:
    runner = FakeClaudeRunner(replies=[VERDICT, VERDICT])
    investigate.investigate_batch(
        make_batch(), runner, conn, tmp_path, concurrency=1, model="claude-haiku-4-5-20251001"
    )
    assert len(runner.calls) == 1

    # Same issue, same HEAD, different model: must not serve Haiku's answer.
    investigate.investigate_batch(
        make_batch(), runner, conn, tmp_path, concurrency=1, model="claude-opus-5"
    )
    assert len(runner.calls) == 2


def test_same_model_still_hits_the_cache(conn, tmp_path: Path) -> None:
    runner = FakeClaudeRunner(replies=[VERDICT])
    for _ in range(2):
        investigate.investigate_batch(
            make_batch(), runner, conn, tmp_path, concurrency=1, model="claude-opus-5"
        )
    assert len(runner.calls) == 1
