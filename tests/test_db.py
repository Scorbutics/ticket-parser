from __future__ import annotations

import pytest

from ticket_parser import db
from ticket_parser.config import Config


@pytest.fixture
def conn(config: Config):
    return db.connect(config.db_path)


def test_fingerprint_identifies_a_line_not_the_email_carrying_it() -> None:
    a = db.fingerprint_chat_message("15/01/2024, 09:12", "Owner", "login is broken")
    b = db.fingerprint_chat_message("15/01/2024, 09:12", "Owner", "login is broken")
    c = db.fingerprint_chat_message("15/01/2024, 09:13", "Owner", "login is broken")

    assert a == b  # same line re-shared in a second email
    assert a != c


def test_overlapping_selection_only_yields_the_new_lines(conn) -> None:
    first = [
        db.fingerprint_chat_message("09:12", "Owner", "login is broken"),
        db.fingerprint_chat_message("09:13", "Owner", "see screenshot"),
    ]
    with db.transaction(conn):
        db.mark_seen(conn, first, "<email-1@gmx.com>")

    # Second share re-includes the last line for context, plus one new one.
    second = [
        first[1],
        db.fingerprint_chat_message("11:40", "Owner", "also the cart total is wrong"),
    ]
    unseen, seen = db.filter_unseen(conn, second)

    assert unseen == [second[1]]
    assert seen == [first[1]]


def test_marking_seen_twice_is_a_noop(conn) -> None:
    fps = [db.fingerprint_chat_message("09:12", "Owner", "x")]
    with db.transaction(conn):
        db.mark_seen(conn, fps, "<email-1@gmx.com>")
        db.mark_seen(conn, fps, "<email-2@gmx.com>")

    row = conn.execute("SELECT email_id FROM seen_chat_messages").fetchone()
    assert row["email_id"] == "<email-1@gmx.com>"  # first sighting wins


def test_verdict_cache_roundtrip(conn) -> None:
    with db.transaction(conn):
        db.cache_put(
            conn,
            cache_key="k1",
            message_id="<email-1@gmx.com>",
            issue_key="issue-1",
            repo_head="abc123",
            prompt_version="v1",
            payload_json='{"verdict": "confirmed"}',
        )

    assert db.cache_get(conn, "k1") == '{"verdict": "confirmed"}'
    assert db.cache_get(conn, "missing") is None


def test_two_documents_in_the_same_minute_are_not_the_same_line() -> None:
    """A document carries no Media, so its name is the only thing telling two
    uncaptioned attachments apart. Without it the second is dropped as seen."""
    from ticket_parser.models import ChatMessage

    def doc(name: str) -> ChatMessage:
        return ChatMessage(
            seq=0,
            wa_id="",
            chat_id="",
            author_name="Owner",
            timestamp=1773047880,
            text="",
            document_name=name,
        )

    assert doc("specs-1926.docx").fingerprint() != doc("specs-3926.docx").fingerprint()
    assert doc("specs-1926.docx").fingerprint() == doc("specs-1926.docx").fingerprint()
