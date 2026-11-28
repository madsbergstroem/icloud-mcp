"""gate.allow (docs/SENDGATE.md, package 10, "Adding someone to a list on request"): an agent asks the owner to add one person or
chat to a list; the owner approves on the dashboard without a fresh sign-in; the entry is written to the settings, applies at once
and the settings say it came on request. The made-up Parcel plugin brings approval_request to the server. Every name, address,
number and chat is made up.

Each test says what it protects, the wrong result it rejects and what a green run does not prove."""
import json
import os

import pytest

from icloud_mcp import dashboard as D
from icloud_mcp.gate import allow as AL
from icloud_mcp.plugins import ChatRef
from icloud_mcp.plugins import beeper as B
from test_dashboard import api, client, signed_in, web  # noqa: F401  (web is a fixture)
from test_external_approvals import url
from test_plugin_actions import call, parcel, refused  # noqa: F401  (parcel is a fixture)

TOM = "+49 170 555-0101"


def ask(setting, entry, reason="Tom asked me to confirm the pickup time with him directly."):
    return {"action": "gate.allow", "params": {"setting": setting, "entry": entry, "reason": reason}}


def settings_file(tmp_path):
    with open(os.path.join(tmp_path, "settings.json")) as f:
        return json.load(f)


def allow_of(mcp):
    return mcp._icloud_dashboard.sources["external"].registry.get("gate.allow").executor


async def test_a_list_entry_is_asked_for_shown_approved_without_a_fresh_sign_in_and_applies_at_once(web, parcel, tmp_path, monkeypatch):
    """protects=the whole flow of D10-8: queued, a card from the fields, approved with an old sign-in, written, live, origin kept;
    fails_when=approving asks for a fresh sign-in, the entry is not written or not live, the reason can be edited, a second approval
    adds it again, or the settings do not say it came on request; no_claim=that a message to the entry then goes out (it still waits)."""
    app, mcp, *_ = web(PARCEL_DEPOTS="d-17")
    listed = {a["action"]: a for a in (await call(mcp, "approval_list_actions"))["actions"]}
    assert "IMESSAGE_SEND_ALLOWLIST" in listed["gate.allow"]["params_schema"]["properties"]["setting"]["enum"]
    got = await call(mcp, "approval_request", **ask("IMESSAGE_SEND_ALLOWLIST", TOM))
    assert got["status"] == "queued_for_owner_approval" and got["sent"] is False
    monkeypatch.setattr(D, "_STEP_UP", -1)               # every sign-in counts as old
    async with client(app) as c:
        token = await signed_in(c)
        it = (await c.get(url(got["outbox_id"]))).json()
        assert it["title"] == "Add to list" and it["approve_label"] == "Add"
        assert it["rows"] == [["List", "Who may receive an iMessage"], ["Adds", "+491705550101"]]
        assert it["text"].startswith("Tom asked me")
        edit = await c.put(url(got["outbox_id"]) + "/text", json={"sha": it["sha"], "text": "Something else."}, headers=api(token))
        assert edit.status_code >= 400                   # the assistant's reason stays as written
        r = await c.post(url(got["outbox_id"]) + "/approve", json={"sha": it["sha"]}, headers=api(token))
        assert r.status_code == 200, r.text
        assert (await c.post(url(got["outbox_id"]) + "/approve", json={"sha": it["sha"]}, headers=api(token))).status_code == 409
        page = (await c.get("/dashboard/api/settings")).json()
        assert page["restart_needed"] is False            # it applies at once, so the page asks for no restart
        field = next(f for sec in [page] for f in _fields(sec) if f["key"] == "IMESSAGE_SEND_ALLOWLIST")
        assert [(o["entry"], o["item_id"]) for o in field["origins"]] == [("+491705550101", got["outbox_id"])]
        # typing a new entry into the same list by hand still asks for a fresh sign-in
        typed = await c.post("/dashboard/api/settings/helper", json={"values": {"IMESSAGE_SEND_ALLOWLIST": "+491705550101,+491705550202"}},
                             headers=api(token))
        assert typed.status_code == 403 and typed.json().get("confirm") is True
    assert settings_file(tmp_path)["IMESSAGE_SEND_ALLOWLIST"] == "+491705550101"
    assert allow_of(mcp).s.imessage_send_allowlist == ("+491705550101",)
    assert (await call(mcp, "approval_status", id=got["outbox_id"]))["status"] == "sent"


def _fields(page):
    """Every field of a settings page as the app gets it, wherever the page nests them."""
    out, todo = [], [page]
    while todo:
        x = todo.pop()
        if isinstance(x, dict):
            if "key" in x and "kind" in x:
                out.append(x)
            todo.extend(x.values())
        elif isinstance(x, list):
            todo.extend(x)
    return out


@pytest.mark.parametrize("entry", ["*", "@example.org", "tom@example.org,sam@example.org", ""])
async def test_more_than_one_person_is_refused_and_nothing_queued(web, parcel, entry):
    """protects=one request, one person (D10-2 for lists); fails_when=*, a domain, several or nothing is queued;
    no_claim=that the owner cannot type them in Settings (they can)."""
    _app, mcp, *_ = web(PARCEL_DEPOTS="d-17", SEND_ALLOWLIST="sam@example.org")
    got = await refused(mcp, **ask("SEND_ALLOWLIST", entry))
    assert got["reason"] in ("policy", "schema") and got["sent"] is False
    assert [q for q in mcp._icloud_dashboard.sources["external"].pending()] == []


async def test_a_list_that_cannot_be_asked_for_an_empty_anyone_list_and_a_listed_entry_are_refused(web, parcel):
    """protects=which lists (D10-9), the empty list that means anyone, an entry already covered; fails_when=OWNER_ADDRESSES can be
    asked for, adding to an empty mail list (which would shut everyone else out) is queued, or a listed entry or its domain is
    queued again; no_claim=every list key."""
    _app, mcp, *_ = web(PARCEL_DEPOTS="d-17", SEND_ALLOWLIST="sam@example.org,@example.net")
    assert (await refused(mcp, **ask("OWNER_ADDRESSES", "tom@example.org")))["reason"] == "schema"
    assert "Already allowed" in (await refused(mcp, **ask("SEND_ALLOWLIST", "Sam@Example.org")))["message"]
    assert "Already allowed" in (await refused(mcp, **ask("SEND_ALLOWLIST", "jana@example.net")))["message"]
    assert "Already allowed" in (await refused(mcp, **ask("INVITE_ALLOWLIST", "tom@example.org")))["message"]   # empty: anyone
    assert (await refused(mcp, **ask("SEND_ALLOWLIST", "not an address")))["reason"] == "policy"
    assert mcp._icloud_dashboard.sources["external"].pending() == []


async def test_an_entry_set_only_in_the_environment_keeps_its_entries(web, parcel, tmp_path):
    """protects=criterion 12: the entry is added to the value in effect; fails_when=the dashboard value replaces the environment's
    list with the new entry alone; no_claim=a list changed in the environment after the approval."""
    app, mcp, *_ = web(PARCEL_DEPOTS="d-17", SEND_ALLOWLIST="sam@example.org")
    got = await call(mcp, "approval_request", **ask("SEND_ALLOWLIST", "Tom@Example.org"))
    async with client(app) as c:
        token = await signed_in(c)
        it = (await c.get(url(got["outbox_id"]))).json()
        assert (await c.post(url(got["outbox_id"]) + "/approve", json={"sha": it["sha"]}, headers=api(token))).status_code == 200
    assert settings_file(tmp_path)["SEND_ALLOWLIST"] == "sam@example.org,tom@example.org"
    assert "tom@example.org" in allow_of(mcp).s.send_allowlist


async def test_an_entry_added_meanwhile_by_hand_is_not_added_twice(web, parcel, tmp_path):
    """protects=the executor reads the list as it is when approved; fails_when=a second copy is written or the approval fails;
    no_claim=two approvals at the same instant in two processes."""
    app, mcp, *_ = web(PARCEL_DEPOTS="d-17", SEND_ALLOWLIST="sam@example.org")
    got = await call(mcp, "approval_request", **ask("SEND_ALLOWLIST", "tom@example.org"))
    with open(os.path.join(tmp_path, "settings.json"), "w") as f:
        json.dump({"SEND_ALLOWLIST": "sam@example.org,tom@example.org"}, f)
    async with client(app) as c:
        token = await signed_in(c)
        it = (await c.get(url(got["outbox_id"]))).json()
        r = await c.post(url(got["outbox_id"]) + "/approve", json={"sha": it["sha"]}, headers=api(token))
        assert r.status_code == 200, r.text
    assert settings_file(tmp_path)["SEND_ALLOWLIST"] == "sam@example.org,tom@example.org"


def test_origins_name_only_entries_still_on_the_list(tmp_path):
    """protects=an entry removed by hand shows no origin; fails_when=a removed entry still says "on request"; no_claim=none."""
    with open(os.path.join(tmp_path, AL.ORIGINS_FILE), "w") as f:
        json.dump([{"setting": "SEND_ALLOWLIST", "entry": "tom@example.org", "label": "tom@example.org", "item_id": "a1", "at": 1},
                   {"setting": "SEND_ALLOWLIST", "entry": "sam@example.org", "label": "sam@example.org", "item_id": "a2", "at": 2}], f)
    assert [o["entry"] for o in AL.origins(str(tmp_path), "SEND_ALLOWLIST", "Tom@example.org,jana@example.org")] == ["tom@example.org"]
    assert AL.origins(str(tmp_path), "INVITE_ALLOWLIST", "tom@example.org") == []


class _S:
    def __init__(self, groups="false", nets="*"):
        self.values = {B.GROUPS: groups, B.NETWORKS: nets}

    def value(self, key, default=""):
        return self.values.get(key, default)


@pytest.mark.parametrize("ref,groups,nets,want", [
    (ChatRef("4817", "Tom", "Signal", False), "false", "*", {"entry": "4817", "label": "Tom · Signal · one to one"}),
    (ChatRef("4818", "Book club", "WhatsApp", True), "false", "*", "group chats are off"),
    (ChatRef("4818", "Book club", "WhatsApp", True), "true", "*", {"entry": "4818", "label": "Book club · WhatsApp · group"}),
    (ChatRef("4817", "Tom", "Signal", False), "false", "WhatsApp", "Writing on Signal is off"),
    (ChatRef("9999", found=False), "false", "*", "does not know this chat"),
])
def test_a_beeper_chat_is_named_by_beeper_and_refused_when_it_cannot_be_written_to(monkeypatch, ref, groups, nets, want):
    """protects=the card names the chat as Beeper does, and a chat that could never be written to is not queued; fails_when=a group
    passes while group chats are off, a chat on a closed network passes, or an unknown id is queued; no_claim=the live gateway."""
    monkeypatch.setattr(B, "_resolve", lambda s, ids: ("ok", [ref]))
    got = B._entry_info(_S(groups, nets), ref.id)
    assert got == want if isinstance(want, dict) else want in got["problem"]


def test_a_beeper_lookup_that_fails_queues_nothing(monkeypatch):
    """protects=nothing is queued when Beeper cannot be asked; fails_when=the entry is queued unnamed; no_claim=none."""
    monkeypatch.setattr(B, "_resolve", lambda s, ids: ("unreachable", []))
    with pytest.raises(RuntimeError):
        B._entry_info(_S(), "4817")


def test_the_beeper_policy_reads_the_list_each_time():
    """protects=a group added on request applies at once; fails_when=the policy keeps the list it read at start;
    no_claim=the gateway's own checks."""
    s = _S(groups="true")
    check = B._policy(s)
    assert check({"network": "WhatsApp", "is_group": True, "chat_id": "4818"})
    s.values[B.ALLOW] = "4818"
    assert check({"network": "WhatsApp", "is_group": True, "chat_id": "4818"}) is None


# -- the review of 10a (Codex, 2026-11-27): one unit each, on the real ListAllow with a settings file in tmp_path --------------
def _allow(tmp_path, monkeypatch, info=None, **env):
    from icloud_mcp import config as C
    from icloud_mcp import settings as ST
    for k in ("SEND_ALLOWLIST", "INVITE_ALLOWLIST", "IMESSAGE_SEND_ALLOWLIST"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = C.Settings.from_env()
    specs = dict(ST.specs())
    if info is not None:
        import dataclasses
        specs["IMESSAGE_SEND_ALLOWLIST"] = dataclasses.replace(specs["IMESSAGE_SEND_ALLOWLIST"], entry_info=info)
    return AL.ListAllow(s, ST.SettingsStore(str(tmp_path)), lambda: specs, ST.current, C.add_live, ST.all_groups)


def _req(setting, entry, label=None):
    from icloud_mcp.gate import actions as GA
    return GA.ActionRequest("r1", "gate.allow", {"setting": setting, "entry": entry, "reason": "x", "list_label": "L",
                                                 "entry_label": label or entry})


def test_a_dashboard_save_between_read_and_write_is_kept(tmp_path, monkeypatch):
    """protects=the entry is added to the list as it is under the file's lock; fails_when=an owner's save made while the approval
    runs is overwritten; no_claim=two processes on one data folder."""
    allow = _allow(tmp_path, monkeypatch)
    allow.store.update({"SEND_ALLOWLIST": "alice@example.org"})
    real = allow.store.change

    def racing(fn):
        allow.store.update({"SEND_ALLOWLIST": "alice@example.org,carol@example.org"})     # the owner saves just before the lock
        return real(fn)
    monkeypatch.setattr(allow.store, "change", racing)
    assert allow.execute(_req("SEND_ALLOWLIST", "bob@example.org")).status == "sent"
    assert allow.store.read()["SEND_ALLOWLIST"] == "alice@example.org,carol@example.org,bob@example.org"


def test_a_running_list_that_lets_anyone_through_is_not_narrowed(tmp_path, monkeypatch):
    """protects=a server that started with an empty mail list (anyone) keeps letting anyone through; fails_when=the live list
    becomes the one approved entry; no_claim=what applies after the restart (the saved list, as the owner set it)."""
    allow = _allow(tmp_path, monkeypatch)
    allow.store.update({"SEND_ALLOWLIST": "alice@example.org"})       # saved on the dashboard, not applied yet
    assert allow.execute(_req("SEND_ALLOWLIST", "bob@example.org")).status == "sent"
    assert allow.s.send_allowlist == () and allow.store.read()["SEND_ALLOWLIST"] == "alice@example.org,bob@example.org"


def test_a_failed_origin_note_still_applies_the_entry(tmp_path, monkeypatch):
    """protects=a saved entry is applied even when the "On request" note cannot be written; fails_when=the approval reports a
    failure or the running list misses the entry; no_claim=the note itself (it is missing then)."""
    allow = _allow(tmp_path, monkeypatch, IMESSAGE_SEND_ALLOWLIST="+491705550202")
    monkeypatch.setattr(allow, "_remember", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    got = allow.execute(_req("IMESSAGE_SEND_ALLOWLIST", "+491705550101"))
    assert got.status == "sent" and "+491705550101" in allow.s.imessage_send_allowlist


def test_the_target_is_asked_again_at_approval(tmp_path, monkeypatch):
    """protects=an entry the service no longer accepts is not added, and a renamed chat waits again with its new name;
    fails_when=a chat on a network closed meanwhile is added, or the card keeps a stale name; no_claim=the live gateway."""
    from icloud_mcp.gate import actions as GA
    seen = {"problem": None, "label": "Tom · Signal · one to one"}
    allow = _allow(tmp_path, monkeypatch, info=lambda s, e: {"entry": e, "label": seen["label"], "problem": seen["problem"]})
    p = _req("IMESSAGE_SEND_ALLOWLIST", "4817", "Tom · Signal · one to one").params
    assert allow.recheck(p) is None
    seen["label"] = "Tom B. · Signal · one to one"
    assert allow.recheck(p)["entry_label"] == "Tom B. · Signal · one to one"
    seen["problem"] = "Writing on Signal is off"
    with pytest.raises(GA.Rechecked):
        allow.recheck(p)


def test_a_phone_number_on_the_imessage_list_says_what_else_it_lets_in():
    """protects=the card is honest about iMessage's nine-digit matching; fails_when=a phone entry shows no caution, or an address
    does; no_claim=that the matching itself is right for every country."""
    assert "705550101" in AL._warnings({"setting": "IMESSAGE_SEND_ALLOWLIST", "entry": "+491705550101"})[0]
    assert AL._warnings({"setting": "IMESSAGE_SEND_ALLOWLIST", "entry": "tom@example.org"}) == []
    assert AL._warnings({"setting": "SEND_ALLOWLIST", "entry": "+491705550101"}) == []
