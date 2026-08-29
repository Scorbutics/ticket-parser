from __future__ import annotations

import json

from ticket_parser import enrich
from ticket_parser.claude_cli import ClaudeError, FakeClaudeRunner
from ticket_parser.models import Batch, ChatMessage


def make_batch(*texts: str) -> Batch:
    messages = [
        ChatMessage(
            seq=index,
            wa_id=f"msg{index}",
            chat_id="chat",
            author_name="Product Owner",
            timestamp=1_700_000_000 + index * 60,
            text=text,
        )
        for index, text in enumerate(texts, start=1)
    ]
    return Batch(batch_id="test", messages=messages)


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

    prompt = enrich.build_prompt([message])

    assert "the export button crashes the whole app" in prompt
    assert "voice transcript" in prompt


def test_clusters_messages_into_issues() -> None:
    batch = make_batch("login fails", "here is the screen", "cart total is wrong")
    runner = FakeClaudeRunner(
        replies=[
            json.dumps(
                [
                    {
                        "message_ids": [1, 2],
                        "title": "Login fails on staging",
                        "category": "auth",
                        "severity": "high",
                    },
                    {
                        "message_ids": [3],
                        "title": "Cart total miscalculated",
                        "category": "payments",
                        "severity": "medium",
                    },
                ]
            )
        ]
    )

    enrich.cluster(batch, runner)

    assert [issue.title for issue in batch.issues] == [
        "Login fails on staging",
        "Cart total miscalculated",
    ]
    assert [len(issue.messages) for issue in batch.issues] == [2, 1]
    assert batch.issues[0].category == "auth"
    assert batch.issues[0].severity == "high"


def test_clustering_runs_with_no_tools() -> None:
    batch = make_batch("login fails")
    runner = FakeClaudeRunner(replies=['[{"message_ids": [1], "title": "x"}]'])

    enrich.cluster(batch, runner)

    assert runner.calls[0]["allowed_tools"] == ()
    assert runner.calls[0]["cwd"] is None


def test_bogus_id_is_dropped_and_reported() -> None:
    batch = make_batch("login fails")
    runner = FakeClaudeRunner(
        replies=[
            json.dumps(
                [{"message_ids": [1, 99], "title": "Login fails", "category": "auth"}]
            )
        ]
    )

    enrich.cluster(batch, runner)

    assert len(batch.issues) == 1
    assert [m.seq for m in batch.issues[0].messages] == [1]
    assert any("unknown message id 99" in w for w in batch.warnings)


def test_a_message_the_model_ignored_still_reaches_the_pdf() -> None:
    batch = make_batch("login fails", "unrelated chatter")
    runner = FakeClaudeRunner(
        replies=[json.dumps([{"message_ids": [1], "title": "Login fails"}])]
    )

    enrich.cluster(batch, runner)

    rendered = [m.seq for issue in batch.issues for m in issue.messages]
    assert sorted(rendered) == [1, 2]
    assert batch.issues[-1].title == "Uncategorised messages"


def test_llm_disabled_gives_one_flat_issue() -> None:
    batch = make_batch("login fails", "cart broken")

    enrich.cluster(batch, None, enabled=False)

    assert len(batch.issues) == 1
    assert batch.issues[0].category == "uncategorised"
    assert [m.seq for m in batch.issues[0].messages] == [1, 2]
    assert any("disabled" in w for w in batch.warnings)


def test_cli_failure_degrades_to_chat_order() -> None:
    batch = make_batch("login fails", "cart broken")
    runner = FakeClaudeRunner(error=ClaudeError("claude not found on PATH"))

    enrich.cluster(batch, runner)

    assert len(batch.issues) == 1
    assert len(batch.issues[0].messages) == 2
    assert any("Categorisation failed" in w for w in batch.warnings)


def test_garbage_reply_degrades_instead_of_raising() -> None:
    batch = make_batch("login fails")
    runner = FakeClaudeRunner(replies=["I'm afraid I can't do that"])

    enrich.cluster(batch, runner)

    assert len(batch.issues) == 1
    assert len(batch.issues[0].messages) == 1


def test_prompt_carries_ids_and_forbids_reproducing_text() -> None:
    batch = make_batch("login is broken")
    prompt = enrich.build_prompt(batch.messages)

    assert "[1] Product Owner: login is broken" in prompt
    assert "Never invent one" in prompt
    assert "Do NOT reproduce" in prompt
