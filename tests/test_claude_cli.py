from __future__ import annotations

import subprocess

import pytest

from ticket_parser.claude_cli import (
    ALWAYS_DENIED,
    REPO_READONLY_TOOLS,
    ClaudeError,
    FakeClaudeRunner,
    SubprocessClaudeRunner,
    extract_json,
)


def test_argv_pins_model_and_tool_policy() -> None:
    runner = SubprocessClaudeRunner("claude", model="claude-opus-5")
    argv = runner._argv("hello", REPO_READONLY_TOOLS)

    assert argv[:2] == ["claude", "-p"]
    assert "--output-format" in argv and "json" in argv
    assert argv[argv.index("--model") + 1] == "claude-opus-5"

    allowed = argv[argv.index("--allowed-tools") + 1]
    assert "Read" in allowed and "Bash(git log:*)" in allowed
    assert "Edit" not in allowed and "Write" not in allowed

    denied = argv[argv.index("--disallowed-tools") + 1]
    for tool in ALWAYS_DENIED:
        assert tool in denied


def test_categorisation_gets_no_tools_at_all() -> None:
    runner = SubprocessClaudeRunner("claude")
    argv = runner._argv("hello", ())
    assert argv[argv.index("--allowed-tools") + 1] == ""


def test_extract_json_handles_fenced_and_chatty_replies() -> None:
    assert extract_json('[{"id": 7}]') == [{"id": 7}]
    assert extract_json('```json\n[{"id": 7}]\n```') == [{"id": 7}]
    assert extract_json('Sure!\n[{"id": 7}]\nHope that helps') == [{"id": 7}]
    with pytest.raises(ClaudeError):
        extract_json("no json here at all")


def test_fake_runner_records_calls() -> None:
    runner = FakeClaudeRunner(replies=['{"ok": true}'])
    result = runner.run("prompt", allowed_tools=REPO_READONLY_TOOLS, label="issue-1")

    assert result.text == '{"ok": true}'
    assert runner.calls[0]["label"] == "issue-1"
    with pytest.raises(ClaudeError):
        runner.run("again")


def test_per_call_model_overrides_the_runner_default() -> None:
    runner = SubprocessClaudeRunner("claude", model="claude-sonnet-5")

    argv = runner._argv("hello", (), model="claude-haiku-4-5-20251001")
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5-20251001"

    argv = runner._argv("hello", ())
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"


def test_no_model_anywhere_leaves_it_to_the_cli() -> None:
    argv = SubprocessClaudeRunner("claude")._argv("hello", ())
    assert "--model" not in argv


def test_an_alias_is_tied_to_the_cli_version_for_caching(monkeypatch) -> None:
    from ticket_parser import claude_cli

    runner = claude_cli.SubprocessClaudeRunner("claude")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="2.1.325 (Claude Code)\n", stderr="")

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)

    assert runner.model_identity("claude-opus-5-5") == "claude-opus-5-5"  # pinned: as-is
    assert runner.model_identity("opus") == "opus@claude-cli-2.1.325"
    assert runner.model_identity("sonnet") == "sonnet@claude-cli-2.1.325"
    assert len(calls) == 1  # the version is asked once
