"""Split attached documents into parts that can be grouped like messages.

A document the product owner sends (a Word spec, a PDF of feedback…) often
covers several problems at once, and each of them may also be discussed in
the chat. Keeping the document as one block would force it onto a single
issue. So each document's text is cut into parts at natural boundaries
(headings, list items, blank lines), and each part becomes its own item in the
batch -- with its own id for clustering -- right after the message that carried
the document. An issue page can then mix chat messages and the document
passages they relate to.

Parts are verbatim slices of the document text: nothing is rewritten, only cut.
"""

from __future__ import annotations

import re
from dataclasses import replace

from .models import IMAGE_TOKEN, ChatMessage

TARGET_CHARS = 500  # a part is closed at the next natural boundary past this
LIST_ITEM_MIN_CHARS = 150  # …or at the next list item past this
HARD_CHARS = 2500  # never longer than this, boundary or not
MAX_PARTS = 40  # long documents get proportionally larger parts instead

_LIST_ITEM = re.compile(r"^\s*(?:[-*•▪◦]|\d+[.)]|[a-zA-Z][.)])\s+")


def _looks_like_heading(line: str) -> bool:
    stripped = line.strip()
    if IMAGE_TOKEN.match(stripped):
        return False  # a figure belongs with the text around it
    return (
        0 < len(stripped) <= 80
        and not stripped.endswith((".", ",", ";", ":", "!", "?"))
        and not stripped.startswith("|")
        and any(ch.isalpha() for ch in stripped)
        and not _LIST_ITEM.match(stripped)
    )


def split_document(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    target = max(TARGET_CHARS, len(text) // MAX_PARTS)
    hard = max(HARD_CHARS, target * 3)
    list_min = min(LIST_ITEM_MIN_CHARS, target)

    parts: list[str] = []
    current: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        chunk = "\n".join(current).strip()
        if chunk:
            parts.append(chunk)
        current, size = [], 0

    for line in text.split("\n"):
        blank = not line.strip()
        if current:
            if (blank or _looks_like_heading(line)) and size >= target:
                flush()
            elif _LIST_ITEM.match(line) and size >= list_min:
                flush()
            elif size + len(line) > hard:
                flush()
        if blank and not current:
            continue
        current.append(line)
        size += len(line) + 1
    flush()
    return parts


def expand_documents(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Messages, each followed by the parts of the document it carried.

    The carrying message keeps its document name, and `document_parts` tells
    the prompts and the template that the text lives in the parts.
    """
    expanded: list[ChatMessage] = []
    for message in messages:
        expanded.append(message)
        if not message.document_text or message.document_part_of:
            continue
        parts = split_document(message.document_text)
        message.document_parts = len(parts)
        for index, chunk in enumerate(parts, start=1):
            expanded.append(
                replace(
                    message,
                    wa_id=f"{message.wa_id}#doc{index}",
                    text=chunk,
                    media=None,
                    media_error="",
                    note="",
                    transcript="",
                    transcript_error="",
                    document_text="",
                    document_parts=0,
                    document_part_of=message.wa_id,
                    part_index=index,
                    part_count=len(parts),
                    # Not a message directory of its own: nothing to move on
                    # commit. Its figures are in the carrying message's.
                    source_dir=None,
                    asset_dir=message.asset_dir or message.source_dir,
                )
            )
    return expanded
