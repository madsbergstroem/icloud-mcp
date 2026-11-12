"""Reading Beeper chats for messages_list_chats, messages_read_chat and messages_search (a messages.Backend).

Everything is read live through the gateway with the read key, on the server entry BEEPER_READ_SERVER (`search_chats`, `get_chat`,
`list_messages`, `search_messages`, `get_accounts`), and nothing is stored. The owner's rules work as iMessage's do: chats hidden
by id (BEEPER_HIDDEN_CHATS), "only these chats" (BEEPER_VISIBLE_CHATS), a maximum age (BEEPER_MAX_AGE_DAYS) and service senders
hidden (BEEPER_HIDE_SHORT_CODES). The messages hub applies the lists and the age to everything that comes back and adds the notice
that the texts are other people's; this backend also asks Beeper for no more than the age allows. A service sender is a one-to-one
chat whose title is a short number (fewer than nine digits); Beeper names no handle that would tell more.

The log has one line per read: the tool, the backend, a hash of the rules in force, the count and the result. Never a title, a chat
id, a name or a text.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Callable

from ...messages import Account, Chat, ChatFilter, Filters, Match, Matches, Message, MessagesError, Page, Person, make_id, split_id
from . import gateway as G

log = logging.getLogger(__name__)

READ, HIDDEN, VISIBLE, MAX_AGE, SHORT_CODES = ("BEEPER_READ_CHATS", "BEEPER_HIDDEN_CHATS", "BEEPER_VISIBLE_CHATS", "BEEPER_MAX_AGE_DAYS",
                                               "BEEPER_HIDE_SHORT_CODES")
PREFIX = "beeper"
PAGE_CALLS = 10                 # list_messages pages (about 20 messages each) one read may ask for
_TRUE = ("1", "true", "yes", "on")


def _list(s: Any, key: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in s.plugin_value(key, "").split(",") if x.strip())


def reading_on(s: Any) -> bool:
    """Off unless the owner turned it on; and it needs the gateway and the read key."""
    return bool(s.beeper_gateway_url and s.beeper_read_key and s.plugin_value(READ, "false").strip().lower() in _TRUE)


def _max_age(s: Any) -> int:
    try:
        return max(0, min(int(s.plugin_value(MAX_AGE, "0") or 0), 36500))
    except ValueError:
        return 0


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def short_code(title: str, kind: str) -> bool:
    """A service sender as far as Beeper shows one: a one-to-one chat named by a short number only (banks, deliveries, codes)."""
    t = (title or "").strip()
    return kind == "single" and bool(t) and re.fullmatch(r"[\d\s+\-()]+", t) is not None and len(_digits(t)) < 9


class _Rules:
    def __init__(self, s: Any):
        self.hidden = {x.casefold() for x in _list(s, HIDDEN)}
        self.visible = {x.casefold() for x in _list(s, VISIBLE)}
        self.max_age = _max_age(s)
        self.short_codes = s.plugin_value(SHORT_CODES, "true").strip().lower() in _TRUE
        rules = json.dumps([sorted(self.hidden), sorted(self.visible), self.max_age, self.short_codes])
        self.hash = hashlib.sha256(rules.encode()).hexdigest()[:12]      # which rules were in force, without naming a chat

    def shown(self, native: str) -> bool:
        k = native.strip().casefold()
        return k not in self.hidden and (not self.visible or k in self.visible)

    def floor(self) -> str | None:
        """The oldest time that may be read, in UTC as Beeper takes it."""
        if not self.max_age:
            return None
        return (datetime.now().astimezone() - timedelta(days=self.max_age)).astimezone().isoformat(timespec="seconds")


def _later(a: str | None, b: str | None) -> str | None:
    """The later of two ISO times (either may be missing)."""
    if not a or not b:
        return a or b
    try:
        da, db = (datetime.fromisoformat(x.replace("Z", "+00:00")) for x in (a, b))
        if da.tzinfo is None:
            da = da.astimezone()
        if db.tzinfo is None:
            db = db.astimezone()
        return a if da >= db else b
    except ValueError:
        return b


def _plain(text: str) -> str:
    """Beeper's text may hold HTML formatting: the words are kept, the markup is not. Text without tags stays as it is."""
    if "<" not in text:
        return text
    text = re.sub(r"<br\s*/?>|</p>\s*<p[^>]*>", "\n", text, flags=re.I)
    text = _ANCHOR.sub(lambda m: m["url"] if _bare(m["label"]) in (m["url"], "") else f"{m['label']} ({m['url']})", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


_ANCHOR = re.compile(r"""<a\s[^>]*?href=["'](?P<url>[^"']+)["'][^>]*>(?P<label>.*?)</a>""", re.I | re.S)


def _bare(label: str) -> str:
    return re.sub(r"<[^>]+>", "", label).strip()


# -- our cursor: where Beeper's page started, and how many of its newest messages were already given --------------------------------
def _cursor_out(beeper_cursor: str | None, given: int) -> str:
    raw = json.dumps({"c": beeper_cursor, "k": given}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_in(cursor: str) -> tuple[str | None, int]:
    try:
        d = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        c, k = d["c"], int(d["k"])
        if (c is not None and not isinstance(c, str)) or k < 0:
            raise ValueError
        return c, k
    except Exception:  # noqa: BLE001
        raise MessagesError("cursor is not a next_cursor of messages_read_chat: give the next_cursor exactly, or none for the newest page.") from None


# -- search_messages, as Beeper's MCP writes it -----------------------------------------------------------------------------------
_S_CHAT = re.compile(r"^# (?P<title>.+) \(chatID: (?P<id>[^)]+)\)\s*$")
_S_NET = re.compile(r"^Chat on .+? \((?P<account>\S+)\)(?: with (?P<people>.+?))?\.?\s*$")
_S_TYPE = re.compile(r"^\*\*Type\*\*: (?P<type>single|group)\s*$")
_S_DAY = re.compile(r"^## .*?(?P<day>\d{4}-\d{2}-\d{2})")          # "## 2026-11-12 (Thursday)" or "## Today (2026-11-12)"
_S_MSG = re.compile(r"^\*\*(?P<name>.+?)\*\* \((?P<time>\d{1,2}:\d{2}(?::\d{2})?)\): ?(?P<text>.*)$")
_S_LINK = re.compile(r"\s*\(\[open at[^\]]*\]\((?P<link>[^)\s]*)\)\)\s*$", re.I)   # "([open at sort key](/open/<chat>/<sort key>))"
_S_MORE = re.compile(r"^(?:Next page|Found \d+\+)", re.M)


def _ref(link: str) -> str:
    """A search hit's reference from its link. Beeper's MCP links `/open/<chat>/<sort key>`: no message id, so it is given as
    `sortkey:<n>` (unique in the chat, not the message_id messages_read_chat shows). A `/chats/<chat>/<id>` link gives the id."""
    parts = [x for x in link.split("/") if x]
    if len(parts) >= 3 and parts[0] == "open":
        return f"sortkey:{parts[-1]}"[:300]
    return parts[-1][:300] if parts else ""


def _search_hits(text: str) -> tuple[list[dict[str, Any]], bool]:
    """The messages of a search_messages answer, each with its chat, and whether Beeper said there are more. Lines the parser does not
    know are a message's continuation when one is open, else skipped; the chat a message is in is the last `# … (chatID: …)` above."""
    text = re.split(r"^# Using this information", text or "", maxsplit=1, flags=re.M)[0]
    more = _S_MORE.search(text) is not None
    hits: list[dict[str, Any]] = []
    chat: dict[str, Any] | None = None
    day, open_msg = "", None
    for line in text.splitlines():
        if m := _S_CHAT.match(line):
            chat, open_msg = {"id": m["id"].strip(), "title": " ".join(m["title"].split())[:300], "account": "", "kind": ""}, None
            continue
        if line.startswith("# "):
            open_msg = None
            continue
        if chat is not None and (m := _S_NET.match(line)):
            chat["account"] = m["account"][:200]
            if not chat["kind"] and m["people"]:
                chat["kind"] = "group" if len([x for x in m["people"].split(", ") if x.strip()]) > 2 else "single"
            continue
        if chat is not None and (m := _S_TYPE.match(line)):
            chat["kind"] = m["type"]
            continue
        if m := _S_DAY.match(line):
            day, open_msg = m["day"], None
            continue
        if chat is not None and (m := _S_MSG.match(line)):
            body, link = m["text"], _S_LINK.search(m["text"])
            if link:
                body = body[:link.start()]
            name = m["name"].strip()
            mine = name.endswith("(You)")
            time_ = m["time"] if m["time"].count(":") == 2 else m["time"] + ":00"
            ref = _ref(link["link"]) if link else ""
            # Beeper writes the day and the time in UTC (10:43 for 10:43:32Z)
            open_msg = {"chat": chat, "name": "" if mine else name[:200], "mine": mine, "at": f"{day}T{time_.zfill(8)}Z" if day else None,
                        "text": body, "id": ref, "linked": bool(link)}
            hits.append(open_msg)
            continue
        if open_msg is not None and line.strip() and not open_msg["linked"]:
            link = _S_LINK.search(line)
            open_msg["text"] += "\n" + (line[:link.start()] if link else line)
            if link:
                open_msg["id"], open_msg["linked"] = _ref(link["link"]), True
    return hits, more


class BeeperBackend:
    """A messages.Backend for Beeper. `make_http` is for tests (a gateway in the process)."""
    name = PREFIX

    def __init__(self, s: Any, make_http: Callable[[dict[str, str], float], Any] | None = None):
        self.s = s
        self.rules = _Rules(s)
        self.gateway = G.read_gateway(s, make_http)
        self.tools = {n: G.read_tool(s, n) for n in ("search_chats", "get_chat", "list_messages", "search_messages")}
        self._accounts = G.accounts_reader(s, make_http) if make_http else G.accounts_reader(s)

    # -- the hub's side ----------------------------------------------------------------------------------------------------
    def filters(self) -> Filters:
        return Filters(shown=self.rules.shown, max_age_days=self.rules.max_age)

    def _logged(self, op: str, run: Callable[[], Any], count: Callable[[Any], int]) -> Any:
        try:
            got = run()
        except MessagesError:
            log.info("chat read: tool=%s backend=beeper filters=%s count=0 result=refused", op, self.rules.hash)
            raise
        except G.GatewayError as e:
            result = {"auth": "refused_by_gateway", "unreachable": "unreachable"}.get(e.kind, "failed")
            log.info("chat read: tool=%s backend=beeper filters=%s count=0 result=%s", op, self.rules.hash, result)
            raise MessagesError("Beeper could not be read just now (the gateway " + ("turned the read key away" if e.kind == "auth" else
                                "did not answer") + "). Do not retry at once; tell the owner if it stays so.") from None
        log.info("chat read: tool=%s backend=beeper filters=%s count=%d result=ok", op, self.rules.hash, count(got))
        return got

    def _call(self, tool: str, args: dict[str, Any]) -> G.Answer:
        answer = self.gateway.call(self.tools[tool], args)
        if answer.is_error:
            raise MessagesError("Beeper turned the request away. Check the chat_id (take it from messages_list_chats) and the dates.")
        return answer

    @staticmethod
    def _text(answer: G.Answer) -> str:
        d = answer.data
        return answer.text or (d.get("result") if isinstance(d, dict) and isinstance(d.get("result"), str) else "")

    def accounts(self) -> list[Account]:
        return [Account(n["account_id"], n["network"], PREFIX) for n in self._accounts.read()["networks"]]

    # -- listing ------------------------------------------------------------------------------------------------------------
    def list_chats(self, flt: ChatFilter) -> list[Chat]:
        def run() -> list[Chat]:
            asked = 200 if (self.rules.hidden or self.rules.visible or self.rules.short_codes) else flt.limit
            args: dict[str, Any] = {"limit": max(1, min(asked, 200))}
            if flt.query and flt.query.strip():
                args["query"] = flt.query.strip()
            if flt.kind:
                args["type"] = flt.kind
            if flt.unread:
                args["unreadOnly"] = True
            if flt.account:
                args["accountIDs"] = [flt.account]
            if after := _later(flt.since, self.rules.floor()):
                args["lastActivityAfter"] = after
            got = G._chats_of(self._call("search_chats", args))
            if got is None:
                raise MessagesError("Beeper's answer was not a list of chats.")
            out = []
            for native, c in got:
                kind = "group" if c.is_group else "single"
                if self.rules.short_codes and short_code(c.title, kind):
                    continue
                out.append(Chat(make_id(PREFIX, native), c.title, kind, c.account, last_at=c.last_activity or None,
                                unread=1 if flt.unread else 0, details={"network": c.network, "people": c.people}))
            return out
        return self._logged("list_chats", run, len)

    # -- reading one chat ------------------------------------------------------------------------------------------------------
    def _chat(self, native: str) -> Chat:
        c = G._chat_of(self._call("get_chat", {"chatID": native, "maxParticipantCount": 0}), native)
        kind = "group" if c is not None and c.is_group else "single"
        if c is None or (self.rules.short_codes and short_code(c.title, kind)):
            raise MessagesError(f"No conversation with chat_id '{make_id(PREFIX, native)}' (take it from messages_list_chats).")
        return Chat(make_id(PREFIX, native), c.title, kind, c.account, last_at=c.last_activity or None, details={"network": c.network})

    def _page(self, native: str, beeper_cursor: str | None) -> tuple[list[dict[str, Any]], str | None, bool]:
        """One page of list_messages, newest first: (items, the cursor for older ones, whether there are older ones)."""
        args: dict[str, Any] = {"chatID": native, **({"cursor": beeper_cursor, "direction": "before"} if beeper_cursor else {})}
        answer = self._call("list_messages", args)
        d = answer.data if isinstance(answer.data, dict) and isinstance(answer.data.get("items"), list) else None
        if d is None:                                         # Beeper's MCP gives the JSON as text (or wrapped as {"result": text})
            try:
                d = json.loads(self._text(answer))
            except ValueError:
                d = None
        if not isinstance(d, dict) or not isinstance(d.get("items"), list):
            raise MessagesError("Beeper's answer was not a list of messages.")
        items = [x for x in d["items"] if isinstance(x, dict) and not x.get("isDeleted")]
        items.sort(key=lambda x: (str(x.get("timestamp") or ""), str(x.get("sortKey") or "")), reverse=True)
        older = d.get("oldestCursor") if isinstance(d.get("oldestCursor"), str) else None
        return items, older, bool(d.get("hasMore")) and older is not None

    @staticmethod
    def _message(chat_id: str, x: dict[str, Any]) -> Message:
        mine = bool(x.get("isSender"))
        sender = None if mine else Person(str(x.get("senderID") or "")[:300], str(x.get("senderName") or "")[:200])
        details = {k: v for k, v in (("type", x.get("type") if x.get("type") not in (None, "TEXT") else None),) if v}
        return Message(str(x.get("id") or "")[:300], chat_id, str(x.get("timestamp") or "") or None, sender, mine,
                       _plain(str(x.get("text") or "")), str(x["linkedMessageID"]) if x.get("linkedMessageID") else None, details)

    def read_chat(self, chat_id: str, limit: int, cursor: str | None) -> Page:
        _name, native = split_id(chat_id)

        def run() -> Page:
            chat = self._chat(native)
            page_cursor, skip = _cursor_in(cursor) if cursor else (None, 0)
            floor = self.rules.floor()
            kept: list[dict[str, Any]] = []
            next_cursor = None
            for _ in range(PAGE_CALLS):
                items, older, more = self._page(native, page_cursor)
                fresh = items[skip:]
                room = limit - len(kept)
                if len(fresh) > room:                         # this page is given in part: the next read starts in it
                    kept += fresh[:room]
                    next_cursor = _cursor_out(page_cursor, skip + room)
                    break
                kept += fresh
                skip = 0
                too_old = floor is not None and bool(fresh) and _later(str(fresh[-1].get("timestamp") or ""), floor) == floor
                if not more or too_old:                       # nothing older, or nothing older the owner allows
                    break
                page_cursor = older
                if len(kept) >= limit:
                    next_cursor = _cursor_out(page_cursor, 0)
                    break
            else:
                next_cursor = _cursor_out(page_cursor, 0)     # the page budget is spent; older messages are there
            kept.reverse()                                    # oldest first
            return Page(chat, tuple(self._message(chat_id, x) for x in kept), next_cursor)
        return self._logged("read_chat", run, lambda p: len(p.messages))

    # -- searching ----------------------------------------------------------------------------------------------------------
    def search(self, query: str, chat_id: str | None, since: str | None, before: str | None, limit: int) -> Matches:
        def run() -> Matches:
            args: dict[str, Any] = {"query": query, "limit": max(1, min(limit, 50))}
            if chat_id:
                args["chatIDs"] = [split_id(chat_id)[1]]
            if after := _later(since, self.rules.floor()):
                args["dateAfter"] = after
            if before:
                args["dateBefore"] = before
            hits, more = _search_hits(self._text(self._call("search_messages", args)))
            found = []
            for h in hits:
                c = h["chat"]
                kind = c["kind"] or "single"
                if self.rules.short_codes and short_code(c["title"], kind):
                    continue
                cid = make_id(PREFIX, c["id"])
                sender = None if h["mine"] else Person("", h["name"])
                found.append(Match(Message(h["id"] or f"at:{h['at']}", cid, h["at"], sender, h["mine"], _plain(h["text"].strip())), c["title"], kind))
            return Matches(tuple(found), None, not more)
        return self._logged("search", run, lambda m: len(m.found))


def backend(s: Any) -> BeeperBackend | None:
    """The backend while the owner has reading on; None leaves Beeper out of the chat tools."""
    return BeeperBackend(s) if reading_on(s) else None
