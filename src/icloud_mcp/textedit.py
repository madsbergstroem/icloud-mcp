"""Editing the text of a waiting request on the dashboard.

The owner can change the words of a request before approving it, and bring back what the agent asked for until it is sent.
Only the text changes: recipients, conversation, calendar and the time limit stay as they were queued. Each kind of request
keeps its text in its own place, so each approval source answers three questions (see ApprovalSource in dashboard.py):

- text_block(q): why the text cannot be edited, or "" when it can
- text_of(q): the text as it stands
- with_text(q, text): the stored message with that text, as (raw bytes, new followup or None)

The edit swaps the queued entry in one step (Outbox.replace, which only succeeds while the entry is still the version the
page showed), so the entry gets a new hash and the old version can no longer be approved. The text the agent asked for stays in
the entry (`edited_from`) until it is sent or discarded. The activity log records that an edit happened, never the text.

  PUT /dashboard/api/items/{key}/text   {"sha": ..., "text": ...}  or  {"sha": ..., "restore": true}
"""
from __future__ import annotations

import asyncio
import email
import hmac
import json
import logging
from email import policy
from typing import Any

from starlette.responses import Response

log = logging.getLogger(__name__)

IMESSAGE_MAX_CHARS = 10000        # the limit messages_send_request has when it queues an iMessage
CALENDAR_MAX_CHARS = 10000
_NOTHING = "Nothing was changed."


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


# -- iMessage: the queued JSON has a `text` field ---------------------------------------------------------------------------
def imessage_text(raw: bytes) -> str:
    return str(json.loads(raw.decode()).get("text", ""))


def imessage_with_text(raw: bytes, text: str) -> bytes:
    d = json.loads(raw.decode())
    d["text"] = text
    return json.dumps(d, ensure_ascii=False).encode()


# -- calendar: the description of the one event in the stored iCalendar object -----------------------------------------------
def _single_event(raw: bytes) -> tuple[Any, Any]:
    from icalendar import Calendar
    cal = Calendar.from_ical(raw)
    events = list(cal.walk("VEVENT"))
    if len(events) != 1:
        raise TextLocked("This change does not hold exactly one event, so its description cannot be edited here.")
    return cal, events[0]


def calendar_block(raw: bytes, followup: dict[str, Any] | None) -> str:
    if (followup or {}).get("op") not in ("create", "update"):
        return "Only a new or changed event has a description to edit. A cancellation or a reply has none."
    try:
        _single_event(raw)
    except TextLocked as e:
        return str(e)
    except Exception:  # noqa: BLE001  -- an event that cannot be read is not edited
        return "This event could not be read, so its description cannot be edited here."
    return ""


def calendar_text(raw: bytes) -> str:
    _, ev = _single_event(raw)
    return str(ev.get("description") or "")


def calendar_with_text(raw: bytes, text: str) -> bytes:
    cal, ev = _single_event(raw)
    if "description" in ev:
        del ev["description"]
    if text:
        ev.add("description", text)
    return cal.to_ical()


# -- mail: the plain-text part of the stored MIME message, when there is no HTML part ----------------------------------------
def _leaves(part: Any) -> Any:
    if part.get_content_type() == "message/rfc822":
        yield part
    elif part.is_multipart():
        for child in part.iter_parts():
            yield from _leaves(child)
    else:
        yield part


def mail_block(raw: bytes) -> str:
    msg = email.message_from_bytes(raw, policy=policy.default)
    if msg.get_content_type() in ("multipart/signed", "multipart/encrypted"):
        return "This mail is signed or encrypted, so its text cannot be edited here."
    if any(p.get_content_type() == "text/html" and p.get_content_disposition() != "attachment" for p in _leaves(msg)):
        return ("This mail also has an HTML version, and only the plain text can be edited here. Changing one would leave "
                "the two different.")
    if msg.get_body(preferencelist=("plain",)) is None:
        return "This mail has no plain-text part to edit."
    return ""


def mail_text(raw: bytes) -> str:
    from .mail import _safe_content
    part = email.message_from_bytes(raw, policy=policy.default).get_body(preferencelist=("plain",))
    return _safe_content(part) if part is not None else ""


def mail_with_text(raw: bytes, text: str) -> bytes:
    from .mail import _set_text
    msg = email.message_from_bytes(raw, policy=policy.default)
    part = msg.get_body(preferencelist=("plain",))
    if part is None:
        raise TextLocked("This mail has no plain-text part to edit.")
    part.clear_content()                  # the payload and the part's Content-* headers; From, To, Subject and the rest stay
    _set_text(part, text)
    return msg.as_bytes(policy=policy.SMTP)


# -- what the app needs ------------------------------------------------------------------------------------------------------
def outbox_of(src: Any) -> Any:
    """The queue a source reads (the same lookup the dashboard uses to hook up its push notes)."""
    return getattr(getattr(src, "mail", None), "outbox", None) or getattr(getattr(src, "cal", None), "outbox", None) or getattr(src, "box", None)


def edit_info(src: Any, q: Any) -> dict[str, Any]:
    """For the request page: whether the text can be edited (and if not, why), the text to start from, and the text the agent
    asked for when the owner has changed it."""
    try:
        reason = src.text_block(q)
        text = "" if reason else src.text_of(q)
    except Exception as e:  # noqa: BLE001  -- an unreadable message is not edited, and the page still opens
        log.warning("Could not read the text of %s %s for editing: %s", src.kind, q.id, e)
        reason, text = "The text of this request could not be read, so it cannot be edited here.", ""
    return {"allowed": not reason, "reason": reason, "text": text, "limit": src.text_limit_of(q),
            "edited": q.edited_from is not None, "original": q.edited_from}


def apply(src: Any, q: Any, text: str | None, restore: bool = False) -> tuple[Any, bool]:
    """Store the new text (or, with restore, the text the agent asked for). Returns (the entry as it is now, whether it changed).
    Raises TextLocked, TextInvalid, or RuntimeError when the entry changed or went away meanwhile."""
    if reason := src.text_block(q):
        raise TextLocked(reason)
    current = src.text_of(q)
    if restore:
        if q.edited_from is None:
            raise TextInvalid("There is nothing to bring back: the text was not edited.")
        text = q.edited_from
    else:
        text = src.clean_text(text)
    original = q.edited_from if q.edited_from is not None else current
    if same(text, current):
        return q, False
    raw, followup = src.with_text(q, text)
    fields: dict[str, Any] = {"edited_from": None if same(text, original) else original}
    if followup is not None:
        fields["followup"] = followup
    new = outbox_of(src).replace(q.id, q.sha256, raw, **fields)
    if new is None:
        raise RuntimeError("The request changed or is no longer waiting. Reload the page.")
    return new, True


def register_text_api(mcp: Any, d: Any, audit: Any) -> None:
    """The PUT route. It uses the dashboard's own guards (session, same origin, CSRF token), set up by register_api."""

    @mcp.custom_route("/dashboard/api/items/{key}/text", methods=["PUT"])
    async def put_text(request: Any) -> Response:
        got = await d.api_writer(request)
        if isinstance(got, Response):
            return got
        _, rec, body = got
        kind, _, item_id = request.path_params["key"].partition(":")
        src = d.sources.get(kind)
        q = next((x for x in src.pending() if x.id == item_id), None) if src else None
        if q is None or not hmac.compare_digest(str(body.get("sha", "")).encode(), q.sha256.encode()):
            return d.api_err("That item is no longer waiting or has changed. Nothing was done.", 409)
        restore = body.get("restore") is True
        ctx = await asyncio.to_thread(src.contexts, [q], False)       # read before the swap: the title for the log line
        v = await asyncio.to_thread(src.describe, q, ctx.get(q.id))
        try:
            new, changed = await asyncio.to_thread(apply, src, q, body.get("text"), restore)
        except TextLocked as e:
            return d.api_err(str(e), 422)
        except TextInvalid as e:
            return d.api_err(f"{e} {_NOTHING}", 422)
        except RuntimeError as e:
            return d.api_err(f"{e} {_NOTHING}", 409)
        if changed:
            what = f"{v['badge'].lower()} “{v['title']}”"
            undone = new.edited_from is None          # "Bring it back", or the original typed in again by hand: either way it is the agent's text again
            audit.record("approval.edit_undone" if undone else "approval.edited", actor="owner", via=rec.get("via", ""),
                         summary=f"Brought back the original text of {what}" if undone else f"Edited {what} before sending",
                         kind=src.kind, outbox_id=q.id, action_type=getattr(q, "action_type", None))
        return d.api_ok({"sha": new.sha256, "changed": changed,
                         "note": ("Brought back what was asked for." if restore else "Saved. It goes out exactly as shown.") if changed else "No change."})
