"""Cluster a batch of messages into issues -- metadata only, never text.

The model receives the messages with stable integer ids and must reply with
JSON describing *which ids belong together* and how to label the group. It
never returns message text, and nothing it returns is rendered as message text:
the PDF builds each message body from `ChatMessage.text`. Any id the model
invents is dropped and logged; any id it forgets lands in a catch-all issue, so
no message can vanish from the report.
"""

from __future__ import annotations

import json
import logging

from .claude_cli import NO_TOOLS, ClaudeError, ClaudeRunner, extract_json
from .models import Batch, ChatMessage, Issue

log = logging.getLogger(__name__)

PROMPT_VERSION = "cluster-v2"

SEVERITIES = {"low", "medium", "high", "critical", "unknown"}

_PROMPT = """You are triaging bug reports sent over WhatsApp by a non-technical \
product owner. Below are the messages, each with a stable integer id.

Group them into distinct issues. Messages about the same problem belong to the \
same issue, including follow-ups and screenshots that illustrate it. A message \
that is pure chatter with no bug content belongs to no issue and should simply \
be omitted from your grouping. A voice note is shown as its transcript, marked \
"(voice transcript, may contain errors)" -- treat it the same as typed text \
when judging what the message is about.

Some items are parts of a document the sender attached (marked "attached \
document …, part i/n"). Treat each part like a message: a document often \
covers several problems, so its parts may belong to different issues, and a \
part belongs with the chat messages about the same problem. Parts with no bug \
content (title page, greetings, table of contents) belong to no issue.

Reply with JSON only. No prose, no code fence. Schema:

[
  {{
    "message_ids": [1, 2],
    "title": "short imperative summary of the problem",
    "category": "one lowercase word, e.g. auth, ui, data, performance, payments",
    "severity": "low" | "medium" | "high" | "critical"
  }}
]

Rules:
- Use ONLY ids that appear below. Never invent one.
- Every id belongs to at most one issue.
- Do NOT reproduce, translate, correct or summarise the message text anywhere \
in your reply. Titles must be your own words.

Messages:
{messages}
"""


# Enough of a document part for grouping; the investigate pass gets it all.
DOCUMENT_PART_CHARS = 1500


def _render_messages(messages: list[ChatMessage]) -> str:
    lines = []
    for message in messages:
        marker = f" [attachment: {message.media.filename}]" if message.media else ""
        text = message.text.strip()
        if not text and message.transcript:
            text = f"(voice transcript, may contain errors) {message.transcript}"
        elif not text:
            text = "(no text)"
        if message.is_document_part:
            body = message.text.strip()
            if len(body) > DOCUMENT_PART_CHARS:
                body = body[:DOCUMENT_PART_CHARS] + " […]"
            lines.append(
                f"[{message.seq}] {message.author_name} (attached document "
                f'"{message.document_name}", part {message.part_index}/{message.part_count}): {body}'
            )
            continue
        if message.document_name:
            marker += f" [document: {message.document_name}"
            marker += f", its text follows as {message.document_parts} part(s)]" if message.document_parts else "]"
        if message.note:
            marker += f" (reviewer note: {' '.join(message.note.split())})"
        lines.append(f"[{message.seq}] {message.author_name}: {text}{marker}")
    return "\n".join(lines)


def build_prompt(messages: list[ChatMessage]) -> str:
    return _PROMPT.format(messages=_render_messages(messages))


def _fallback_issue(messages: list[ChatMessage], title: str) -> Issue:
    return Issue(
        key="issue-1",
        title=title,
        category="uncategorised",
        severity="unknown",
        messages=list(messages),
    )


def cluster(
    batch: Batch,
    runner: ClaudeRunner | None,
    *,
    enabled: bool = True,
    timeout: int | None = None,
    model: str = "",
) -> Batch:
    """Populate `batch.issues`. Degrades to a single flat issue on any failure."""
    if not enabled or runner is None:
        batch.issues = [_fallback_issue(batch.messages, "Uncategorised report")]
        batch.warnings.append("Categorisation is disabled; messages are in chat order.")
        return batch

    try:
        result = runner.run(
            build_prompt(batch.messages),
            allowed_tools=NO_TOOLS,
            timeout=timeout,
            label="cluster",
            model=model,
        )
        payload = extract_json(result.text)
    except ClaudeError as exc:
        log.error("clustering failed, falling back to flat order", extra={"error": str(exc)})
        batch.issues = [_fallback_issue(batch.messages, "Uncategorised report")]
        batch.warnings.append(f"Categorisation failed ({exc}); messages are in chat order.")
        return batch

    batch.issues = _issues_from_payload(payload, batch)
    return batch


def _issues_from_payload(payload: object, batch: Batch) -> list[Issue]:
    by_seq = {message.seq: message for message in batch.messages}
    if not isinstance(payload, list):
        batch.warnings.append("Categorisation returned an unexpected shape; chat order used.")
        return [_fallback_issue(batch.messages, "Uncategorised report")]

    issues: list[Issue] = []
    claimed: set[int] = set()

    for index, raw in enumerate(payload, start=1):
        if not isinstance(raw, dict):
            continue
        ids = raw.get("message_ids") or []
        members: list[ChatMessage] = []
        for candidate in ids if isinstance(ids, list) else []:
            try:
                seq = int(candidate)
            except (TypeError, ValueError):
                continue
            if seq not in by_seq:
                # Hallucinated id: drop it, say so, keep going.
                log.warning("model returned an unknown message id", extra={"id": candidate})
                batch.warnings.append(f"Categorisation referenced unknown message id {candidate}.")
                continue
            if seq in claimed:
                continue
            claimed.add(seq)
            members.append(by_seq[seq])

        if not members:
            continue

        severity = str(raw.get("severity", "unknown")).strip().lower()
        issues.append(
            Issue(
                key=f"issue-{index}",
                title=str(raw.get("title") or "Untitled issue").strip(),
                category=str(raw.get("category") or "uncategorised").strip().lower(),
                severity=severity if severity in SEVERITIES else "unknown",
                messages=sorted(members, key=lambda m: (m.timestamp, m.seq)),
            )
        )

    unclaimed = [m for m in batch.messages if m.seq not in claimed]
    if unclaimed:
        # A message the model ignored still reaches the PDF. Content is never
        # dropped just because the model did not know what to do with it.
        issues.append(
            Issue(
                key=f"issue-{len(issues) + 1}",
                title="Uncategorised messages",
                category="uncategorised",
                severity="unknown",
                messages=unclaimed,
            )
        )
        log.info("messages left uncategorised by the model", extra={"count": len(unclaimed)})

    if not issues:
        return [_fallback_issue(batch.messages, "Uncategorised report")]

    issues.sort(key=lambda issue: min(m.timestamp for m in issue.messages))
    for index, issue in enumerate(issues, start=1):
        issue.key = f"issue-{index}"
    return issues


def verify_no_text_leaked(payload: object, messages: list[ChatMessage]) -> list[str]:
    """Flag any model-authored string that copies a message body.

    Belt and braces: the renderer already ignores model text entirely, but if a
    title turns out to be a copy of the product owner's wording we want it in
    the run report rather than silently in the PDF.
    """
    bodies = {m.text.strip().lower() for m in messages if len(m.text.strip()) > 20}
    leaks: list[str] = []
    for chunk in json.dumps(payload, ensure_ascii=False).lower().split('"'):
        if chunk.strip() in bodies:
            leaks.append(chunk.strip())
    return leaks
