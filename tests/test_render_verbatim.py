"""The verbatim-integrity assertion. This test is the point of the design.

If a model could alter the product owner's wording, the whole document becomes
untrustworthy. The guarantee is structural -- bodies are rendered from
ChatMessage.text and the model's reply is consumed only as ids and labels -- and
these tests are what keep it that way.
"""

from __future__ import annotations

import html
import json
import re

from ticket_parser import enrich, investigate, render
from ticket_parser.claude_cli import FakeClaudeRunner
from ticket_parser.models import Batch, ChatMessage

TRICKY_TEXTS = [
    "Le bouton « Se connecter » ne marche pas 😤",
    "Fails at 09:12 — every time, on iOS *and* Android",
    "cart shows 12,50 € instead of 10,00 €\nand the total is wrong too",
    "<script>alert('xss')</script> & <b>bold</b>",
    "מה קורה עם ההתחברות?",
    "  leading and trailing spaces  ",
]


def make_batch() -> Batch:
    messages = [
        ChatMessage(
            seq=index,
            wa_id=f"msg{index}",
            chat_id="chat",
            author_name="Product Owner",
            timestamp=1_700_000_000 + index * 60,
            text=text,
        )
        for index, text in enumerate(TRICKY_TEXTS, start=1)
    ]
    return Batch(batch_id="20240115T0912", messages=messages)


def bodies_from_html(rendered: str) -> list[str]:
    """Pull the message bodies back out of the rendered document."""
    return [
        html.unescape(match)
        for match in re.findall(
            r'<div class="body">(.*?)</div>', rendered, flags=re.DOTALL
        )
    ]


def test_every_body_is_byte_identical_to_the_source() -> None:
    batch = make_batch()
    enrich.cluster(batch, None, enabled=False)

    rendered = render.render_html(batch)

    assert bodies_from_html(rendered) == [m.text for m in batch.messages]


def test_bodies_survive_a_model_that_tries_to_rewrite_them() -> None:
    """A hostile reply that returns rewritten text must have no effect."""
    batch = make_batch()
    runner = FakeClaudeRunner(
        replies=[
            json.dumps(
                [
                    {
                        "message_ids": [1, 2, 3, 4, 5, 6],
                        "title": "Login broken",
                        "category": "auth",
                        "severity": "high",
                        # None of these fields exist in the renderer's vocabulary.
                        "text": "COMPLETELY DIFFERENT TEXT",
                        "messages": ["rewritten", "also rewritten"],
                        "body": "sanitised version",
                    }
                ]
            )
        ]
    )

    enrich.cluster(batch, runner)
    rendered = render.render_html(batch)

    assert bodies_from_html(rendered) == TRICKY_TEXTS
    assert "COMPLETELY DIFFERENT TEXT" not in rendered
    assert "sanitised version" not in rendered


def test_verdict_prose_cannot_escape_its_paragraph() -> None:
    """The one place model prose IS rendered must still be escaped."""
    batch = make_batch()
    enrich.cluster(batch, None, enabled=False)
    batch.issues[0].verdict = investigate._verdict_from_payload(
        {
            "verdict": "confirmed",
            "observations": "<script>alert(1)</script> the cookie is unset",
            "suggested_fix": "set it",
        },
        "abc1234",
    )

    rendered = render.render_html(batch)

    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert bodies_from_html(rendered) == TRICKY_TEXTS


def test_html_escaping_does_not_corrupt_the_source_data() -> None:
    batch = make_batch()
    enrich.cluster(batch, None, enabled=False)

    render.render_html(batch)

    assert [m.text for m in batch.messages] == TRICKY_TEXTS


def test_one_page_per_issue_plus_a_cover() -> None:
    batch = make_batch()
    runner = FakeClaudeRunner(
        replies=[
            json.dumps(
                [
                    {"message_ids": [1, 2], "title": "Login", "category": "auth"},
                    {"message_ids": [3], "title": "Cart", "category": "payments"},
                    {"message_ids": [4, 5, 6], "title": "Misc", "category": "ui"},
                ]
            )
        ]
    )
    enrich.cluster(batch, runner)

    rendered = render.render_html(batch)

    assert rendered.count('<section class="page">') == 4  # cover + 3 issues


def test_images_are_inlined_as_data_uris(tmp_path) -> None:
    from ticket_parser.models import Media

    # A 1x1 PNG.
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c6300010000050001"
        "0d0a2db40000000049454e44ae426082"
    )
    image = tmp_path / "IMG-1.png"
    image.write_bytes(png)

    batch = make_batch()
    batch.messages[0].media = Media("IMG-1.png", "image/png", image)
    enrich.cluster(batch, None, enabled=False)

    rendered = render.render_html(batch)

    assert 'src="data:image/png;base64,' in rendered
    assert "IMG-1.png" not in rendered.split('src="data:image/png;base64,')[1][:100]


def test_voice_note_renders_its_transcript_not_a_data_uri(tmp_path) -> None:
    from ticket_parser.models import Media

    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"fake-ogg")

    batch = make_batch()
    batch.messages[0].media = Media("voice.ogg", "audio/ogg", audio)
    batch.messages[0].transcript = "le bouton ne marche pas"
    enrich.cluster(batch, None, enabled=False)

    rendered = render.render_html(batch)

    assert '<div class="body transcript">le bouton ne marche pas</div>' in rendered
    assert "data:audio" not in rendered
    assert bodies_from_html(rendered) == TRICKY_TEXTS


def test_voice_note_without_a_transcript_shows_the_error_instead(tmp_path) -> None:
    from ticket_parser.models import Media

    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"fake-ogg")

    batch = make_batch()
    batch.messages[0].media = Media("voice.ogg", "audio/ogg", audio)
    batch.messages[0].transcript_error = "could not transcribe voice note: boom"
    enrich.cluster(batch, None, enabled=False)

    rendered = render.render_html(batch)

    assert "could not transcribe voice note: boom" in rendered
