"""Outbox: outgoing messages wait here until the owner releases them on the dashboard (or /outbox without it).

The MCP token cannot tell the owner from an injected instruction, so sending is split in two: an agent can only
*queue* a message; releasing it is the owner's step, in a browser (the dashboard, or /outbox). Queued items are stored as the
exact raw message that will be sent, expire on their own, and can be released at most once. The agent that queued one can take
it back while it waits (approval_withdraw.py), which claims it the same way a release does.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import callctx


class OutboxFull(Exception):
    pass


@dataclass
class QueuedMessage:
    id: str
    created_at: float
    expires_at: float
    raw: bytes
    recipients: list[str]
    followup: dict[str, Any] | None   # e.g. {"folder": "INBOX", "uid": 7, "flag": "\\Answered"} applied after a successful send
    sha256: str
    key: str | None = None            # what the message was made from (e.g. one saved draft), so asking again finds this entry
    edited_from: str | None = None    # the text the agent asked for, kept while the owner's edited text is what waits (textedit.py)
    requested_by: str | None = None   # the name the queueing client gave itself (a label against mix-ups, not a credential)
    action_type: str | None = None    # which registered kind of action this is, for a source that holds several (actions.py); the entry's own bytes say it too

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "created_at": self.created_at, "expires_at": self.expires_at,
                "raw": base64.b64encode(self.raw).decode(), "recipients": self.recipients,
                "followup": self.followup, "sha256": self.sha256, **({"key": self.key} if self.key else {}),
                **({"edited_from": self.edited_from} if self.edited_from is not None else {}),
                **({"requested_by": self.requested_by} if self.requested_by else {}),
                **({"action_type": self.action_type} if self.action_type else {})}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "QueuedMessage":
        raw = base64.b64decode(d["raw"])
        if hashlib.sha256(raw).hexdigest() != d["sha256"]:
            raise ValueError("outbox entry failed its integrity check")
        return cls(d["id"], d["created_at"], d["expires_at"], raw, list(d["recipients"]), d.get("followup"), d["sha256"], d.get("key"),
                   d.get("edited_from"), d.get("requested_by"), d.get("action_type"))


class Outbox:
    def __init__(self, data_dir: str, ttl: int, max_items: int, filename: str = "outbox.json"):
        self.path = Path(data_dir) / filename
        self.ttl, self.max_items = ttl, max_items
        self._lock = threading.RLock()
        self._items: dict[str, QueuedMessage] = {}
        self.on_new: Any = None            # called with each newly queued item (the dashboard's push note), never on a repeat
        self._on_expired: Any = None       # called once with each item that timed out unanswered (see on_expiry)
        self._lapsed: list[QueuedMessage] = []   # timed out before anything listened: reported when something does
        self._load()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except Exception:  # noqa: BLE001  -- unreadable state must never turn into a send
            return
        for d in data.get("items", []):
            try:
                q = QueuedMessage.from_json(d)
            except Exception:  # noqa: BLE001  -- skip tampered/corrupt entries
                continue
            self._items[q.id] = q
        self._prune()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".outbox-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"items": [q.to_json() for q in self._items.values()]}, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _prune(self) -> None:
        now = time.time()
        gone = [v for v in self._items.values() if v.expires_at <= now]
        if not gone:
            return
        self._items = {k: v for k, v in self._items.items() if v.expires_at > now}
        try:
            self._save()                   # written out, so a restart does not find the same entries and report them twice
        except OSError:
            pass
        self._lapsed.extend(gone)
        self._report_lapsed()

    def on_expiry(self, callback: Any) -> None:
        """Call back with each item that times out unanswered, once. Items that timed out before this was set (while the server
        was down, or before it started listening) are reported now."""
        self._on_expired = callback
        self._report_lapsed()

    def _report_lapsed(self) -> None:
        if self._on_expired is None:
            return
        lapsed, self._lapsed = self._lapsed, []
        for q in lapsed:
            try:
                self._on_expired(q)
            except Exception:  # noqa: BLE001  -- a failed log line must never break the queue
                pass

    # -- operations ------------------------------------------------------------
    def add(self, raw: bytes, recipients: list[str], followup: dict[str, Any] | None = None, key: str | None = None,
            approval_page: str = "the dashboard's Approvals page (/outbox when the dashboard is off)",
            requested_by: str | None = None, action_type: str | None = None) -> QueuedMessage:
        """Queue a message, or return the entry already waiting for the same bytes or the same key. A key names what the message
        was built from (one saved draft), for when rebuilding it gives different bytes (a fresh Date or Message-ID).
        requested_by: the queueing client's name; left out, it is the one of the tool call running on this thread.
        action_type: the registered kind of action, for entries of the external source (actions.py); the others leave it out."""
        with self._lock:
            self._prune()
            sha = hashlib.sha256(raw).hexdigest()
            same = next((q for q in self._items.values() if q.sha256 == sha or (key is not None and q.key == key)), None)
            if same is not None:                 # the same message is already waiting: one entry, not two
                return same
            if len(self._items) >= self.max_items:
                raise OutboxFull(f"{len(self._items)} messages are already waiting for owner approval (OUTBOX_MAX={self.max_items}). "
                                 "Nothing was queued. Do not retry: tell the owner to review the queue on "
                                 f"{approval_page}, where they can send or discard what is waiting.")
            now = time.time()
            label = (requested_by if requested_by is not None else callctx.client()) or None
            q = QueuedMessage(secrets.token_urlsafe(12), now, now + self.ttl, raw, recipients, followup, sha, key, requested_by=label,
                              action_type=action_type)
            self._items[q.id] = q
            self._save()
        if self.on_new is not None:
            try:
                self.on_new(q)
            except Exception:  # noqa: BLE001  -- a failed note must never fail the queueing
                pass
        return q

    def pending(self) -> list[QueuedMessage]:
        with self._lock:
            self._prune()
            return sorted(self._items.values(), key=lambda q: q.created_at)

    def claim(self, item_id: str) -> QueuedMessage | None:
        """Atomically remove and return an item, so it can be released at most once."""
        with self._lock:
            self._prune()
            q = self._items.pop(item_id, None)
            if q:
                self._save()
            return q

    def replace(self, item_id: str, sha256: str, raw: bytes, **fields: Any) -> QueuedMessage | None:
        """Swap a waiting item's message for a new version of it (a file the owner or an upload added). Only when the item is
        still the version the caller read, so two uploads at once cannot both win; the new bytes get their own hash, which
        the approval page then shows and checks. fields: other parts of the entry that change with it (followup, edited_from)."""
        with self._lock:
            self._prune()
            q = self._items.get(item_id)
            if q is None or q.sha256 != sha256:
                return None
            q = replace(q, raw=raw, sha256=hashlib.sha256(raw).hexdigest(), **fields)
            self._items[item_id] = q
            self._save()
            return q

    def restore(self, q: QueuedMessage) -> None:
        """Put a claimed item back (used when the send itself failed, so the owner can retry)."""
        with self._lock:
            self._items[q.id] = q
            self._save()
