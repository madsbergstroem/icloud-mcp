"""The interface every kind of approval request implements, so the dashboard lists, shows, edits, approves and discards them
with one set of code (dashboard.py), and approval_status and approval_withdraw answer for all of them."""
from __future__ import annotations

from typing import Any

from .. import textedit as T


# ---------------------------------------------------------------------------------------------------------------------
# Approval sources: one interface for queued mail, iMessages and (with ALLOW_CALENDAR_INVITES=approve) calendar changes
# ---------------------------------------------------------------------------------------------------------------------
class ApprovalSource:
    kind = ""
    badge = ""
    approve_label = "Approve"

    def pending(self) -> list[Any]:
        raise NotImplementedError

    def describe(self, q: Any, context: Any = None) -> dict[str, Any]:
        """{'title', 'badge', 'rows': [(label, value)], 'text', 'notes': [str], 'warnings': [str], 'needs_override': bool}"""
        raise NotImplementedError

    def contexts(self, items: list[Any], prune: bool = True) -> dict[str, Any]:
        """prune=True only with the whole queue: it forgets what is kept for requests not in items."""
        return {}

    def release(self, q: Any) -> str:
        """Do it; return a one-line result for the page. Raises on failure (and puts the item back)."""
        raise NotImplementedError

    def discard(self, q: Any) -> None:
        raise NotImplementedError

    # Editing the text before approving (textedit.py). A source that has no text of its own keeps these answers.
    text_limit = 10000

    def text_limit_of(self, q: Any) -> int:
        """The most characters this request's text may have; a source whose requests differ in it says so here."""
        return self.text_limit

    def text_block(self, q: Any) -> str:
        """Why the text cannot be edited, or "" when it can."""
        return "This kind of request has no text to edit."

    def text_of(self, q: Any) -> str:
        raise T.TextLocked(self.text_block(q))

    def clean_text(self, text: Any) -> str:
        return T.clean(text, allow_empty=False, limit=self.text_limit)

    def with_text(self, q: Any, text: str) -> tuple[bytes, dict[str, Any] | None]:
        """The stored message with this text, and the changed followup (None when it stays)."""
        raise T.TextLocked(self.text_block(q))
