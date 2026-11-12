"""Golden check: what the send tools queue is stored, and answered, exactly as before the tools were merged into one.

One server with iMessage sending and the Beeper gateway on gets a fixed run of requests: a new conversation by handle, an existing
chat, the same request again, a group, a request with a reply and a key, and the same key again. For each, the check records the
tool's answer, then every entry as it is stored (the bytes the owner approves: raw, sha256, recipients, key, action type), both from
memory and as read back from the queue files, and what approval_status says of each. Ids and times are replaced by labels. The
result is compared with tests/fixtures/send_requests_golden.json, which was made from the code as it stood before the two send tools
were merged into `messages_send_request` (then `imessage_send_message` and `beeper_send_request`); `send_imessage` and `send_beeper`
below are the only lines that name the tool, and they are the only lines that changed. Run with UPDATE_GOLDEN=1 to write it
again, and only when what is stored is meant to change.
"""
import base64
import copy
import json
import os
from pathlib import Path

from icloud_mcp.contacts import ContactsService

from test_beeper import GROUP, KEY, SINGLE, URL, WORDS, gateway, hub  # noqa: F401  (fixtures)
from test_dashboard import web  # noqa: F401  (a fixture)
from test_hub_golden import _scrub

GOLDEN = Path(__file__).parent / "fixtures" / "send_requests_golden.json"
PEOPLE = [{"name": "Anna Example", "uid": "u-anna", "emails": [{"address": "Anna@Example.org"}], "phones": []}]
CHATS = {"chats": [{"chat_id": "chat900", "name": "Hiking crew", "group": True, "participants": ["+31600000002", "anna@example.org"],
                    "services": ["iMessage"]},
                   {"chat_id": "anna@example.org", "name": "", "group": False, "participants": ["anna@example.org"], "services": ["iMessage"]}]}
ENV = dict(ENABLE_IMESSAGE="true", IMESSAGE_ALLOW_SEND="true", IMESSAGE_SEND_ALLOWLIST="anna@example.org,+31600000002",
           IMESSAGE_NEVER_SEND="bot@example.org", BRIDGE_TOKEN="t" * 40, BEEPER_GATEWAY_URL=URL, BEEPER_EXEC_KEY=KEY,
           # since the group rule (plugins/beeper): * covers single chats only; the group the scenario writes to is listed by id, groups on
           BEEPER_SEND_ALLOWLIST=f"*,{GROUP}", BEEPER_SEND_GROUPS="true")


async def call(mcp, name, args):
    return json.loads((await mcp.call_tool(name, args)).content[0].text)


async def send_imessage(mcp, text, *, chat_id=None, handle=None):
    """The one call that queues an iMessage, whatever the tool is called."""
    return await call(mcp, "messages_send_request", {"text": text, **({"chat_id": f"imessage:{chat_id}"} if chat_id else {}),
                                                     **({"handle": handle} if handle else {})})


async def send_beeper(mcp, chat_id, text, **kw):
    """The one call that queues a Beeper message, whatever the tool is called."""
    return await call(mcp, "messages_send_request", {"chat_id": f"beeper:{chat_id}", "text": text, **kw})


def stored(box):
    """Every entry of a queue as it is kept, without what changes from run to run (id and times), in the order queued."""
    drop = ("id", "created_at", "expires_at")
    return [{k: v for k, v in q.to_json().items() if k not in drop} for q in box._items.values()]


def on_disk(box):
    return [{k: v for k, v in d.items() if k not in ("id", "created_at", "expires_at")} for d in json.loads(box.path.read_text())["items"]]


def steady(value):
    """The record with the one answer field that counts down (expires_in_seconds) reduced to whether time is left."""
    if isinstance(value, dict):
        return {k: ("<time left>" if k == "expires_in_seconds" and value[k] > 0 else steady(v)) for k, v in value.items()}
    return [steady(v) for v in value] if isinstance(value, list) else value


def decoded(entries):
    return [{**e, "raw_text": base64.b64decode(e["raw"]).decode()} for e in entries]


async def _scenario(web, monkeypatch) -> dict:
    monkeypatch.setattr(ContactsService, "_people", lambda self: copy.deepcopy(PEOPLE))
    app, mcp, *_ = web(**ENV)
    mcp._icloud_bridge.call = lambda op, a=None: ({"chat": next(c for c in CHATS["chats"] if c["chat_id"] == a["chat_id"]), "messages": [],
                                                  "complete": True} if op == "imessage_read" else {})
    ids: dict[str, str] = {}
    rec: dict = {"imessage": {}, "beeper": {}}

    def name(label, got):
        ids[got["outbox_id"]] = label
        return got

    im, bp = rec["imessage"], rec["beeper"]
    im["by_handle"] = name("im-handle", await send_imessage(mcp, "Lunch <b>tomorrow</b>?  ÜBER-café", handle="Anna@Example.org"))
    im["in_group"] = name("im-group", await send_imessage(mcp, "See you at the station", chat_id="chat900"))
    im["in_chat"] = name("im-chat", await send_imessage(mcp, "Running late", chat_id="anna@example.org"))
    im["same_again"] = await send_imessage(mcp, "Lunch <b>tomorrow</b>?  ÜBER-café", handle="Anna@Example.org")
    im["refused"] = {}
    for key, args in {"assistant": {"handle": "bot@example.org"}, "not_listed": {"handle": "zed@example.org"}}.items():
        try:
            await send_imessage(mcp, "x", **args)
            im["refused"][key] = "queued"
        except Exception:  # noqa: BLE001  -- the wording of a refusal is not what is pinned; that nothing was queued is
            im["refused"][key] = "refused"
    bp["single"] = name("bp-single", await send_beeper(mcp, SINGLE, WORDS))
    bp["group"] = name("bp-group", await send_beeper(mcp, GROUP, "Meeting moved to nine"))
    bp["with_reply_and_key"] = name("bp-key", await send_beeper(mcp, SINGLE, "Yes, nine is fine", reply_to="msg-1", request_id="ask-1"))
    bp["same_key"] = await send_beeper(mcp, SINGLE, "Yes, nine is fine", reply_to="msg-1", request_id="ask-1")
    boxes = mcp._icloud_outboxes
    im["stored"], im["on_disk"] = decoded(stored(boxes["imessage"])), decoded(on_disk(boxes["imessage"]))
    bp["stored"], bp["on_disk"] = decoded(stored(boxes["external"])), decoded(on_disk(boxes["external"]))
    rec["status"] = {label: await call(mcp, "approval_status", {"id": oid}) for oid, label in ids.items()}
    return _scrub(steady(rec), ids)


async def test_what_the_send_tools_queue_is_stored_as_before(web, hub, gateway, monkeypatch):
    got = await _scenario(web, monkeypatch)
    assert gateway.sent == []                                                       # asking sends nothing
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    want = json.loads(GOLDEN.read_text())
    assert json.loads(json.dumps(got, sort_keys=True)) == want


def test_the_record_really_holds_stored_entries():
    """A golden file that lost its content would pass anything: it holds the bytes of each entry and each answer."""
    want = json.loads(GOLDEN.read_text())
    assert len(want["imessage"]["stored"]) == 3 and len(want["beeper"]["stored"]) == 3
    assert want["imessage"]["refused"] == {"assistant": "refused", "not_listed": "refused"}
    assert want["beeper"]["same_key"]["already_queued"] is True and want["beeper"]["with_reply_and_key"]["status"] == "queued_for_owner_approval"
    assert "ÜBER-café" in want["imessage"]["stored"][0]["raw_text"] and want["beeper"]["stored"][2]["key"] == "ask-1"
    assert want["imessage"]["same_again"]["outbox_id"] == "<id:im-handle>"          # the same bytes: the same entry, not a second
    assert want["status"]["bp-key"]["kind"] == "external" and want["status"]["im-group"]["kind"] == "imessage"
