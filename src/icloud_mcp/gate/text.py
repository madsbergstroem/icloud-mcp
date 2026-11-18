"""The words of a waiting request as the owner edits them: what refuses an edit, and how a text is cleaned before it is stored.

These used to live in textedit.py, next to mail's own helpers; package 6b moves them here, so the gate reads and refuses edits
without importing anything outside gate/ (docs/SENDGATE.md, package 6b, criterion 5). textedit.py keeps the routes and imports
these names from here, so both raise and catch the same classes.
"""
from __future__ import annotations

from typing import Any


class TextLocked(Exception):
    """The text of this request cannot be edited here. The message says why, in words for the owner."""


class TextInvalid(ValueError):
    """The new text is not acceptable (empty, too long). The message says why, in words for the owner."""


def clean(text: Any, *, allow_empty: bool, limit: int, strip: bool = False) -> str:
    """The text as it will be stored: line breaks as \\n, no control characters."""
    if not isinstance(text, str):
        raise TextInvalid("The text is missing.")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if strip:
        text = text.strip()
    if "\x00" in text:
        raise TextInvalid("The text has a character that cannot be sent.")
    if not allow_empty and not text.strip():
        raise TextInvalid("The text is empty.")
    if len(text) > limit:
        raise TextInvalid(f"The text is longer than {limit:,} characters.")
    return text


def same(a: str, b: str) -> bool:
    """Two texts count as the same when only line-break style or the blank space at the end differs."""
    return a.replace("\r\n", "\n").rstrip() == b.replace("\r\n", "\n").rstrip()
