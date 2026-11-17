"""iMessage: the owner's own Messages history, read through the Mac helper, and (when enabled) sending.

Everything read here was written by other people and is untrusted data: results carry the notice and safety warnings, like mail.
Which chats exist to the agent is decided here as well as on the Mac: IMESSAGE_HIDDEN_CHATS and IMESSAGE_VISIBLE_CHATS are applied
before the Mac is asked and again to what comes back, and IMESSAGE_MAX_AGE_DAYS (0 = the whole history) bounds every read. Handles
are matched to the owner's contacts here, never on the Mac: an email exactly, a phone number exactly or by its last nine digits
(match "suffix", which is not a confirmation). A chat with a handle in IMESSAGE_NEVER_SEND (the owner's own assistant, say) is
labelled as such.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Mapping

from .bridge import BridgeError
from .messages import (ASSISTANT_NOTE, NOTICE, Account, Chat, ChatFilter, Filters, Match, Matches, Message, MessagesError, Page, Person,
                       SendRequest, make_id, split_id)
from .safety import compact, warnings_for


class IMessageError(Exception):
    pass


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").casefold()
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _key(handle: str) -> str:
    """One form for comparing handles from settings and from chat.db: emails lower-case, phone numbers as their last nine digits
    (so +31 6..., 06... and 0031 6... match), anything else (a group chat id, a short code) lower-case as given."""
    h = (handle or "").strip()
    if "@" in h:
        return h.lower()
    d = _digits(h)
    return d[-9:] if len(d) >= 9 and re.fullmatch(r"[\s+().\d-]+", h) else h.lower()


def _short_code(chat_id: str) -> bool:
    """A service sender: neither an email nor a full phone number (banks, delivery and one-time-code senders use these)."""
    h = (chat_id or "").strip()
    return "@" not in h and not h.lower().startswith("chat") and len(_digits(h)) < 9


class IMessageService:
    def __init__(self, settings: Any, bridge: Any, contacts: Any | None = None):
        self.s, self.bridge, self.contacts = settings, bridge, contacts

    # -- people ---------------------------------------------------------------------------
    def _people_index(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        emails: dict[str, dict[str, Any]] = {}
        phones: dict[str, dict[str, Any]] = {}
        suffix: dict[str, list[dict[str, Any]]] = {}
        if self.contacts is None:
            return emails, phones, suffix
        try:
            people = self.contacts._people()
        except Exception:  # noqa: BLE001 - contacts unreachable: handles stay unnamed rather than failing the read
            return emails, phones, suffix
        for c in people:
            who = {"name": c["name"], "contact_uid": c["uid"]}
            for e in c.get("emails", []):
                emails.setdefault(e["address"].strip().lower(), who)
            for p in c.get("phones", []):
                d = _digits(p["number"])
                if len(d) >= 6:
                    phones.setdefault(d, who)
                    suffix.setdefault(d[-9:], []).append(who)
        return emails, phones, suffix

    def _resolver(self):
        emails, phones, suffix = self._people_index()

        def resolve(handle: str | None) -> dict[str, Any] | None:
            if not handle:
                return None
            out: dict[str, Any] = {"handle": handle}
            if "@" in handle:
                who = emails.get(handle.lower())
                if who:
                    out.update(who, match="exact")
                return out
            d = _digits(handle)
            if d in phones:
                out.update(phones[d], match="exact")
            elif len(d) >= 9 and len(suffix.get(d[-9:], [])) == 1:
                out.update(suffix[d[-9:]][0], match="suffix")
            return out
        return resolve

    # -- which chats exist ---------------------------------------------------------------------
    def _check_chat(self, chat_id: str) -> None:
        if not self._shown(chat_id):
            raise IMessageError(f"No conversation with chat_id '{chat_id}' (take it from messages_list_chats).")

    def _shown(self, chat_id: str) -> bool:
        k = _key(chat_id)
        if k in {_key(h) for h in self.s.imessage_hidden_chats}:
            return False
        if self.s.imessage_hide_short_codes and _short_code(chat_id):
            return False
        return not self.s.imessage_visible_chats or k in {_key(h) for h in self.s.imessage_visible_chats}

    def _exclude(self) -> str | None:
        return ",".join(self.s.imessage_hidden_chats) or None           # the helper compares by the same last-nine-digits rule

    def _since(self, since: str | None) -> str | None:
        """The later of the caller's 'since' and the IMESSAGE_MAX_AGE_DAYS limit."""
        if not self.s.imessage_max_age_days:
            return since
        floor = (datetime.now() - timedelta(days=self.s.imessage_max_age_days)).replace(microsecond=0)
        if not since:
            return floor.isoformat()
        try:
            given = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as e:
            raise IMessageError(f"'{since}' is not an ISO 8601 date.") from e
        given_naive = given.astimezone().replace(tzinfo=None) if given.tzinfo else given
        return since if given_naive > floor else floor.isoformat()

    def _is_assistant(self, chat_id: str, participants: list[str]) -> bool:
        never = {_key(h) for h in self.s.imessage_never_send}
        return bool(never) and bool({_key(x) for x in (chat_id, *participants) if x} & never)

    def _people_of(self, participants: list[str], resolve) -> list[dict[str, Any]]:
        return [resolve(p) for p in participants]

    # -- tools ------------------------------------------------------------------------------------
    def list_chats(self, *, query: str | None = None, limit: int = 20, since: str | None = None,
                   include_archived: bool = False) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        args = {"limit": 1000 if query else limit, "since": self._since(since), "include_archived": include_archived or None,
                "exclude": self._exclude()}
        got = self.bridge.call("imessage_chats", {k: v for k, v in args.items() if v is not None})
        resolve = self._resolver()
        q = _norm(query or "")
        chats = []
        for c in got.get("chats", []):
            if not self._shown(c["chat_id"]):
                continue
            people = self._people_of(c.get("participants", []), resolve)
            name = c.get("name") or (people[0].get("name", "") if len(people) == 1 else "")
            if q and q not in _norm(" ".join([name, c["chat_id"], *(p.get("handle", "") + " " + p.get("name", "") for p in people)])):
                continue
            chat = {"chat_id": c["chat_id"], "name": name, "group": c.get("group", False), "participants": people,
                    "last_message_at": c.get("last_message_at"), "last_text": c.get("last_text", ""),
                    "last_from_me": c.get("last_from_me"), "unread": c.get("unread", 0), "services": c.get("services", [])}
            if c.get("archived"):
                chat["archived"] = True
            if self._is_assistant(c["chat_id"], c.get("participants", [])):
                chat.update(assistant_thread=True, note=ASSISTANT_NOTE)
            chats.append(chat)
            if len(chats) == limit:
                break
        found = warnings_for(*(c["last_text"] for c in chats))
        return {"notice": NOTICE, "count": len(chats), "chats": chats, **({"safety_warnings": found} if found else {})}

    def read_chat(self, chat_id: str, *, limit: int = 50, before_id: int | None = None, since: str | None = None) -> dict[str, Any]:
        self._check_chat(chat_id)
        args = {"chat_id": chat_id, "limit": max(1, min(int(limit), 500)), "before_id": before_id, "since": self._since(since),
                "exclude": self._exclude()}
        got = self.bridge.call("imessage_read", {k: v for k, v in args.items() if v is not None})
        resolve = self._resolver()
        chat = got.get("chat", {})
        people = self._people_of(chat.get("participants", []), resolve)
        by_handle = {p["handle"]: p for p in people if p}
        messages = got.get("messages", [])
        found = warnings_for(*(m.get("text", "") for m in messages if not m.get("from_me")))
        # Lean messages: a sender in participants is its bare handle (matched to contacts there, once), anyone else the matched
        # dict; an inline reply points at the rowid of its target when that is on this page (the helper's guid 'id' is then
        # not needed); the chat's main service is said once.
        rowid_of = {m.get("id"): m.get("rowid") for m in messages if m.get("id")}
        services = Counter(m.get("service") for m in messages if m.get("service"))
        service = services.most_common(1)[0][0] if services else None
        for i, m in enumerate(messages):
            if m.get("from_me"):
                m.pop("sender", None)
            elif m.get("sender"):
                m["sender"] = m["sender"] if m["sender"] in by_handle else resolve(m["sender"])
            if m.get("reply_to") in rowid_of:
                m["reply_to_rowid"] = rowid_of[m.pop("reply_to")]
            m.pop("id", None)
            if service and m.get("service") == service:
                m.pop("service")
            messages[i] = compact(m, keep=("rowid", "at"))
        out_chat = {"chat_id": chat.get("chat_id", chat_id), "name": chat.get("name") or (people[0].get("name", "") if len(people) == 1 else ""),
                    "group": chat.get("group", False), "participants": people, **({"service": service} if service else {})}
        if self._is_assistant(chat_id, chat.get("participants", [])):
            out_chat.update(assistant_thread=True, note=ASSISTANT_NOTE)
        return {"notice": NOTICE, "chat": out_chat, "messages": messages, "complete": got.get("complete", True),
                **({"older_before_id": got["older_before_id"]} if got.get("older_before_id") else {}),
                **({"safety_warnings": found} if found else {})}

    def search(self, query: str, *, chat_id: str | None = None, limit: int = 20, since: str | None = None,
               before: str | None = None) -> dict[str, Any]:
        if not (query or "").strip():
            raise IMessageError("query is empty.")
        if chat_id:
            self._check_chat(chat_id)
        args = {"query": query.strip(), "chat_id": chat_id, "limit": max(1, min(int(limit), 200)), "since": self._since(since),
                "before": before, "exclude": self._exclude()}
        got = self.bridge.call("imessage_search", {k: v for k, v in args.items() if v is not None})
        resolve = self._resolver()
        matches = [m for m in got.get("matches", []) if self._shown(m.get("chat_id", ""))]
        for m in matches:
            if m.get("sender"):
                m["sender"] = resolve(m["sender"])
        found = warnings_for(*(m.get("text", "") for m in matches if not m.get("from_me")))
        return {"notice": NOTICE, "count": len(matches), "matches": matches, "scanned": got.get("scanned"),
                "complete": got.get("complete", True), **({"safety_warnings": found} if found else {})}

    # -- sending ------------------------------------------------------------------------------
    def _target(self, chat_id: str | None, handle: str | None) -> dict[str, Any]:
        """What a send would go to: the conversation's id, name and participants. Refused for hidden or unknown chats."""
        from .config import _norm_handle
        if bool(chat_id) == bool(handle):
            raise IMessageError("Give exactly one of chat_id (an existing conversation) or handle (an email or +number).")
        if handle:
            h = _norm_handle(handle)
            self._check_chat(h)
            return {"chat_id": None, "handle": h, "name": "", "participants": [h], "group": False}
        self._check_chat(chat_id)
        # One chat by id (the same chat record and participants imessage_chats gives, from any point in the history), not the
        # 1,000 most recent chats with a latest-message query each. A missing chat is a BridgeError ('no conversation').
        got = self.bridge.call("imessage_read", {k: v for k, v in {"chat_id": chat_id, "limit": 1,
                                                                     "exclude": self._exclude()}.items() if v is not None})
        c = got.get("chat") or {}
        if c.get("chat_id") != chat_id:
            raise IMessageError(f"No conversation with chat_id '{chat_id}' (take it from messages_list_chats).")
        return {"chat_id": chat_id, "handle": None, "name": c.get("name", ""), "participants": c.get("participants", []),
                "group": c.get("group", False), "services": c.get("services", [])}

    def _sms_only(self, t: dict[str, Any], people: list[dict[str, Any]]) -> str:
        """Why an SMS-only conversation is refused before it is queued (the Mac would refuse it on approval), naming the same
        person's iMessage conversations that may be sent to, so the agent can send there instead of asking the owner.
        Matched by contact uid only: no text from the address book is searched for or repeated."""
        uids = {p["contact_uid"] for p in people if p.get("contact_uid")}
        alts: list[str] = []
        if uids and not t.get("group"):
            try:
                chats = self.list_chats(limit=200).get("chats", [])
            except Exception:  # noqa: BLE001 - the hint is a courtesy; the refusal stands without it
                chats = []
            for c in chats:
                handles = [p.get("handle", "") for p in c.get("participants", [])]
                if (c.get("group") or c.get("assistant_thread") or "iMessage" not in c.get("services", [])
                        or not c.get("participants") or not {p.get("contact_uid") for p in c["participants"]} <= uids):
                    continue
                try:
                    self._permit({"chat_id": c["chat_id"], "handle": None, "participants": handles})
                except IMessageError:
                    continue
                alts.append(c["chat_id"])
        msg = (f"Conversation '{t['chat_id']}' exists only as SMS, and only iMessage is sent from here. Nothing was sent or "
               "queued. ")
        if alts:
            return msg + "Send to the same person's iMessage conversation instead: chat_id " + ", ".join(f"'{make_id('imessage', a)}'" for a in alts) + "."
        return msg + ("Pick a conversation of this person whose services include iMessage (messages_list_chats), or ask the "
                      "owner for their Apple ID address.")

    def _permit(self, t: dict[str, Any]) -> None:
        """IMESSAGE_NEVER_SEND wins over everything; then IMESSAGE_SEND_ALLOWLIST (empty = nobody, '*' = anyone)."""
        ids = [x for x in (t["chat_id"], t["handle"], *t["participants"]) if x]
        never_keys = {_key(h) for h in self.s.imessage_never_send}
        never = sorted(x for x in ids if _key(x) in never_keys)
        if never:
            raise IMessageError(f"Never sent to {', '.join(never)} (IMESSAGE_NEVER_SEND): that is the owner's own assistant "
                                "or a blocked handle. Nothing was sent.")
        allow = {_key(h) for h in self.s.imessage_send_allowlist if h != "*"}
        if "*" in self.s.imessage_send_allowlist:
            return
        if not allow:
            raise IMessageError("Sending iMessages is on, but IMESSAGE_SEND_ALLOWLIST is empty, so nobody may receive one. Only the "
                                "owner can add people to it. Nothing was sent.")
        if (t["chat_id"] and _key(t["chat_id"]) in allow) or all(_key(p) in allow for p in t["participants"]):
            return
        missing = sorted(p for p in t["participants"] if _key(p) not in allow)
        raise IMessageError(f"Not on IMESSAGE_SEND_ALLOWLIST: {', '.join(missing)}. Tell the owner; only they can change the list. "
                            "Nothing was sent.")

    def send(self, text: str, *, chat_id: str | None = None, handle: str | None = None, public_url: str = "",
             external: Any = None) -> dict[str, Any]:
        """Queue an iMessage for the owner's approval as an action of type imessage.send on `external` (package 4b), or send it
        at once where the owner turned approval off. Nothing is queued without the external source."""
        text = (text or "").strip()
        if not text:
            raise IMessageError("text is empty.")
        if len(text) > 10000:
            raise IMessageError("text is longer than 10,000 characters.")
        t = self._target(chat_id, handle)
        self._permit(t)
        resolve = self._resolver()
        to = {**t, "participants": self._people_of(t["participants"], resolve)}
        to.pop("services", None)
        if t.get("services") and "iMessage" not in t["services"]:
            raise IMessageError(self._sms_only(t, to["participants"]))
        if self.s.imessage_send_requires_approval:
            if self.s.local_mode or external is None:
                return {"status": "not_sent_needs_owner", "sent": False, "to": to, "text": text,
                        "notice": "This server has no approval page here, and every iMessage needs the owner's approval: show the "
                                  "owner the text so they can send it themselves. It was NOT sent."}
            from .gate import actions as A                 # package 4b: an action of type imessage.send on the external source
            try:
                q = external.queue_external(ACTION_ID, params_of({"chat_id": t["chat_id"], "handle": t["handle"], "text": text, "to": to}), None)
            except A.ExternalError as e:
                raise IMessageError(str(e)) from None
            return {"status": "queued_for_owner_approval", "sent": False, "outbox_id": q.id, "to": to,
                    "approve_at": self.s.approval_link("external", q.id),
                    "notice": "Queued, NOT sent: it goes out only when the owner approves it on the approval page (approve_at). "
                              "Say so; do not send it again: approval_status with the outbox_id says what became of it."}
        got = self.bridge.call("imessage_send", {k: v for k, v in {"chat_id": t["chat_id"], "handle": t["handle"], "text": text}.items() if v})
        return {**got, "to": to}

    def recheck(self, p: Mapping[str, Any]) -> Any:
        """The action type's look just before sending (package 4b, the endings of 4a): None when the conversation and the lists are
        as the owner saw them; 'target_changed' when the conversation is gone; 'not_allowed' when the owner hid it since or a
        member is no longer allowed; the parameters with the members as they are now when someone joined or left and everyone is
        allowed (a new version waits). Raises ExternalError, and the request waits again, when the Mac cannot be read."""
        from .gate import actions as A
        if not self._shown(p.get("chat_id") or p.get("handle") or ""):
            return "not_allowed"
        try:
            target = self._target(p.get("chat_id"), p.get("handle"))
        except BridgeError as e:
            if "no conversation" in str(e).lower():
                return "target_changed"
            raise A.ExternalError(str(e)) from None
        except IMessageError:
            return "target_changed"
        try:
            self._permit(target)
        except IMessageError:
            return "not_allowed"
        to = p.get("to") or {}
        then = sorted(_key(x.get("handle", "") if isinstance(x, dict) else x) for x in to.get("participants", []))
        now = sorted(_key(x) for x in target["participants"])
        if p.get("chat_id") and then and now != then:
            fresh = {**target, "participants": self._people_of(target["participants"], self._resolver())}
            fresh.pop("services", None)
            return params_of({"chat_id": p.get("chat_id"), "handle": p.get("handle"), "text": p.get("text", ""), "to": fresh})
        return None


# -- the action type (docs/SENDGATE.md, package 4b) -----------------------------------------------------------------------------
ACTION_ID = "imessage.send"
NEVER_REACHED = ("did not pick up", "has not connected", "last seen", "or newer: update the helper", "Nothing was sent")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chat_id": {"type": "string", "minLength": 1},
        "handle": {"type": "string", "minLength": 1},
        "text": {"type": "string", "minLength": 1, "maxLength": 10000, "pattern": "\\S"},      # not only spaces
        # filled in by this server from the Mac, never by an agent (the tool takes only chat_id, handle and text)
        "to": {"type": "object"},
        "people": {"type": "string"},
        "chat": {"type": "string"},
    },
    "required": ["text", "to"],
    "additionalProperties": False,
}


def _people_line(to: Mapping[str, Any]) -> str:
    return ", ".join(f"{x.get('name')} ({x.get('handle')})" if x.get("name") else x.get("handle", "")
                     for x in to.get("participants", []) if isinstance(x, dict))


def params_of(d: Mapping[str, Any]) -> dict[str, Any]:
    """The parameters of an iMessage request from what the queue stored before 4b: {chat_id, handle, text, to}."""
    to = dict(d.get("to") or {})
    p: dict[str, Any] = {"text": d.get("text") or "", "to": to}
    for k in ("chat_id", "handle"):
        if d.get(k):
            p[k] = d[k]
    if people := _people_line(to):
        p["people"] = people
    if to.get("group") and d.get("chat_id"):
        p["chat"] = d["chat_id"]
    return p


def convert(raw: bytes) -> bytes:
    """A request stored before 4b, in the canonical form of the action type (a new hash, D4-1)."""
    import json
    from .gate import actions as A
    return A.canonical(ACTION_ID, params_of(json.loads(raw.decode())))


def label_of(p: Mapping[str, Any]) -> str:
    """Who the request reaches, as the list and the log name it: the chat's name, else its people, else its id."""
    to = p.get("to") or {}
    return to.get("name") or p.get("people") or p.get("chat_id") or p.get("handle") or ""


def _action_type() -> Any:
    from .gate import actions as A
    group = lambda p: bool((p.get("to") or {}).get("group"))         # noqa: E731
    return A.ActionType(
        id=ACTION_ID, title="iMessage", badge="iMessage", approve_label="Send", params_schema=SCHEMA,
        rows=(("Chat with", "people"), ("Chat ID", "chat")), text_field="text",
        recipients=lambda p: [label_of(p)],
        warnings=lambda p: ["A group conversation: everyone in it receives this."] if group(p) else [],
        tags=lambda p: ["Group"] if group(p) else [], text_style="bubble",
        confirm_twice=lambda p: f"Everyone in {label_of(p) or 'the conversation'} receives it." if group(p) else None,
        note="Sent from your Mac once you approve.")


class IMessageExecutor:
    """Sends an approved request from the owner's Mac. Called from a worker thread, once per approval, after the type's recheck
    found the conversation and the lists as the owner saw them."""

    def __init__(self, service: IMessageService):
        self.svc = service

    def execute(self, request: Any) -> Any:
        from .gate import actions as A
        p = request.params
        try:
            got = self.svc.bridge.call("imessage_send", {k: v for k, v in {"chat_id": p.get("chat_id"), "handle": p.get("handle"),
                                                                            "text": p.get("text")}.items() if v})
        except BridgeError as e:
            if any(x in str(e) for x in NEVER_REACHED):           # the Mac never had it, or refused before Messages did
                return A.Result("failed", f"Not sent, still waiting: {e}")
            return A.Result("unclear", "The Mac did not answer clearly, so it may or may not have gone out. Nothing is sent again; "
                                       "look in Messages first.")
        status = got.get("status") if isinstance(got, dict) else None
        if status == "sent":
            return A.Result("sent", f"Sent: iMessage to {label_of(p)}.")
        word = "Not delivered" if status == "failed" else "Handed to Messages (not yet confirmed)"
        return A.Result("unclear", f"{word}: iMessage to {label_of(p)}. Nothing is sent again; look in Messages first.")


def register_type(registry: Any, service: IMessageService) -> None:
    """imessage.send on the external source. The owner's lists are checked by the service (never-send, the list, hidden chats)
    when a request is queued and again in the recheck, so the registry's own list lets everyone through."""
    registry.register(_action_type(), IMessageExecutor(service), ["*"], recheck=service.recheck,
                      channel_tool="messages_send_request", builtin=True)


class IMessageBackend:
    """The owner's Messages app as a backend of the messages hub (messages.py). Everything that reads stays in IMessageService:
    the Mac helper call, the matching of handles to contacts, the owner's lists (hidden chats, "only these chats", maximum age,
    service senders), the errors. This class only changes the shape of what the service returns, and tells the hub how to apply the
    same lists on its side."""
    name = "imessage"
    ACCOUNT = Account("imessage", "iMessage and SMS on this Mac", "imessage")

    def __init__(self, service: IMessageService):
        self.svc = service

    def accounts(self) -> list[Account]:
        return [self.ACCOUNT]

    def filters(self) -> Filters:
        """The service's own tests of the owner's lists, handed to the hub, and the maximum age as it is now."""
        return Filters(shown=self.svc._shown, assistant=self.svc._is_assistant, max_age_days=self.svc.s.imessage_max_age_days)

    @staticmethod
    def _person(p: dict[str, Any] | str) -> Person:
        p = {"handle": p} if isinstance(p, str) else p
        return Person(p["handle"], p.get("name", ""), p.get("match", ""), p.get("contact_uid", ""))

    def list_chats(self, flt: ChatFilter) -> list[Chat]:
        wide = flt.kind is not None or flt.unread is not None       # the hub narrows by these afterwards: look at as many as can be asked for
        got = self.svc.list_chats(query=flt.query, limit=200 if wide else flt.limit, since=flt.since)
        return [Chat(make_id(self.name, c["chat_id"]), c["name"], "group" if c["group"] else "single", self.ACCOUNT.account,
                     c["last_message_at"], c["unread"], c["last_text"], c["last_from_me"],
                     tuple(self._person(p) for p in c["participants"] if p),
                     details=compact({"services": c["services"], "archived": c.get("archived")}))
                for c in got["chats"]]

    def read_chat(self, chat_id: str, limit: int, cursor: str | None) -> Page:
        try:
            before = int(cursor) if cursor else None
        except ValueError:
            raise MessagesError("cursor is the next_cursor of an earlier answer, copied exactly.") from None
        try:
            got = self.svc.read_chat(split_id(chat_id)[1], limit=limit, before_id=before)
        except BridgeError as e:
            raise _missing(chat_id, e) from None
        c = got["chat"]
        people = tuple(self._person(p) for p in c["participants"] if p)
        by_handle = {p.handle: p for p in people}
        messages = []
        for m in got["messages"]:
            sender = m.get("sender")
            who = None if m.get("from_me") or not sender else (by_handle.get(sender) if isinstance(sender, str) else self._person(sender))
            reply = m.get("reply_to_rowid", m.get("reply_to"))
            rest = {k: v for k, v in m.items() if k not in ("rowid", "at", "from_me", "sender", "text", "reply_to", "reply_to_rowid")}
            messages.append(Message(str(m["rowid"]), chat_id, m.get("at"), who, bool(m.get("from_me")), m.get("text", ""),
                                    None if reply is None else str(reply), rest))
        chat = Chat(make_id(self.name, c["chat_id"]), c["name"], "group" if c["group"] else "single", self.ACCOUNT.account,
                    participants=people, details=compact({"service": c.get("service")}))
        return Page(chat, tuple(messages), str(got["older_before_id"]) if got.get("older_before_id") else None)

    def search(self, query: str, chat_id: str | None, since: str | None, before: str | None, limit: int) -> Matches:
        try:
            got = self.svc.search(query, chat_id=split_id(chat_id)[1] if chat_id else None, limit=limit, since=since, before=before)
        except BridgeError as e:
            raise _missing(chat_id or "", e) from None
        found = []
        for m in got["matches"]:
            who = m.get("sender")
            rest = {k: v for k, v in m.items() if k not in _SEARCH_CORE}
            found.append(Match(Message(str(m["rowid"]), make_id(self.name, m["chat_id"]), m.get("at"), IMessageBackend._person(who) if who else None,
                                       bool(m.get("from_me")), m.get("text", ""), details=rest),
                               m.get("chat_name", ""), "group" if m.get("group") else "single"))
        return Matches(tuple(found), got.get("scanned"), got.get("complete", True))


_SEARCH_CORE = ("id", "rowid", "at", "from_me", "sender", "text", "reply_to", "chat_id", "chat_name", "group")


def _missing(chat_id: str, e: BridgeError) -> Exception:
    """The Mac says a conversation does not exist in its own words, which name a tool this server no longer has. Said here instead.
    Anything else the Mac says is passed on as it is."""
    if "no conversation" in str(e).lower():
        return MessagesError(f"No conversation with chat_id '{chat_id}' (take it from messages_list_chats).")
    return e


class IMessageSender:
    """Queues an iMessage for the owner's approval through the service, as an action of type imessage.send (package 4b). A reply
    and a key are things an iMessage request does not have, so a request that asks for them is refused instead of quietly sent
    without."""
    name = "imessage"
    takes_handle = True

    def __init__(self, service: IMessageService, outbox: Any, public_url: str, external: Any = None):
        """outbox: unused since package 4b (requests wait on `external`); kept so the server's call stays as it is."""
        self.svc, self.public_url, self.external = service, public_url, external

    def send_request(self, req: SendRequest) -> dict[str, Any]:
        if req.reply_to or req.request_id:
            raise MessagesError("reply_to and request_id are for Beeper chats only. An iMessage request has neither: leave them out. "
                                "A repeat of an identical request that still waits returns that request; ask approval_status before asking again.")
        try:
            return self.svc.send(req.text, chat_id=req.chat_id, handle=req.handle, public_url=self.public_url, external=self.external)
        except BridgeError as e:
            raise _missing(make_id(self.name, req.chat_id) if req.chat_id else "", e) from None
