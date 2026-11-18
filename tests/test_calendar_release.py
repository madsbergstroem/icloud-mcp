"""Package 5a (docs/SENDGATE.md): every way an approved calendar change can end is a state of the approval store, and nothing
is written to iCloud twice. Runs the real caldav client against the in-memory iCloud-shaped server of caldav_fakes, which
honours If-Match and If-None-Match, so a count of PUT and DELETE requests is a count of writes. Since package 5b a held change is
an action of one of the calendar types on the external source: the endings of 5a come from the type's recheck and executor."""
import dataclasses

import niquests
import pytest

import caldav_fakes
import icloud_mcp.cal as cal_mod
from icloud_mcp.cal import CalendarError, CalendarService
from icloud_mcp.config import Settings
from icloud_mcp.gate import actions as A
from icloud_mcp.gate.actions import Rechecked
from icloud_mcp.gate.external import ExternalApprovals
from icloud_mcp.gate.store import Store

ME = "me@icloud.com"
EVENT = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Example//EN\r\nBEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20261101T090000Z\r\n"
         "DTSTART:20261205T100000Z\r\nDTEND:20261205T120000Z\r\nSUMMARY:Book club\r\nORGANIZER:mailto:{org}\r\n"
         "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:jana@example.org\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


class NoTicker:
    def add(self, fn):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME=ME, ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16, ALLOW_CALENDAR_INVITES="approve",
                     INVITE_ALLOWLIST="jana@example.org,@club.example.org").items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cal_mod, "TICKER", NoTicker())
    s = Settings.from_env()
    server = caldav_fakes.Server(password=s.app_password)

    class Client(cal_mod.caldav.DAVClient):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.session.request = lambda *a, **kw: server(*a, **kw)   # looked up per call, so a test can wrap the server
    monkeypatch.setattr(cal_mod.caldav, "DAVClient", Client)
    cal = CalendarService(dataclasses.replace(s, default_timezone="Europe/Berlin"))
    reg = A.ActionRegistry()
    cal_mod.register_types(reg, cal)
    cal.external = ExternalApprovals(Store(str(tmp_path), "external", 3600, 20), reg)     # package 5b: the calendar types
    return cal, server


def approve(cal, item_id, sha256=None):
    """The owner's approval of a waiting change, as the dashboard does it; sha256: the version the owner looked at. Returns
    {"status": sent | unclear, "note"}; raises Rechecked when it ended at the read, CalendarError when it waits again."""
    q = next((x for x in cal.external.box.pending() if x.id == item_id), None)
    if q is None:
        raise CalendarError("That change is no longer waiting.")
    try:
        got = cal.external.release(dataclasses.replace(q, sha256=sha256) if sha256 else q)
    except (A.Rechecked, A.Revised):
        raise
    except A.ExternalError as e:                             # it waits again: the words the owner reads
        raise CalendarError(str(e)) from None
    return {"status": {"unconfirmed": "unclear"}.get(got.delivery, got.delivery), "note": str(got)}


def writes(server):
    return [x for x in server.log if x[0] in ("PUT", "DELETE")]


def invite(cal, **kw):
    r = cal.create_event(summary="Autumn fair", start="2026-12-05T10:00", end="2026-12-05T12:00", calendar="Calendar",
                         attendees=kw.pop("attendees", ["jana@example.org"]), **kw)
    assert r["status"] == "queued_for_owner_approval"
    return r["outbox_id"]


def seeded_update(cal, server, uid="bc1", org=ME):
    server.events["home"][f"{uid}.ics"] = (EVENT.format(uid=uid, org=org), '"e900"')
    r = cal.update_event(uid, summary="Book club, new room")
    assert r["status"] == "queued_for_owner_approval"
    return r["outbox_id"]


def state(cal, item_id):
    return cal.external.box.state_of(item_id)


def test_an_approved_invitation_is_written_once_with_its_type_and_ends_sent(env):
    """protects=the 2xx row, and the type that outlasts the content; fails_when=no PUT, two PUTs, another state, no type,
    or the line still claims iCloud emailed; no_claim=that iCloud delivers the email (the fake has no scheduling)."""
    cal, server = env
    item = invite(cal, description="x" * 5000)
    r = approve(cal, item)
    assert r["status"] == "sent" and "was asked to email jana@example.org" in r["note"]
    assert state(cal, item) == ("sent", None) and len(writes(server)) == 1
    body = next(iter(server.events["home"].values()))[0]
    assert body.replace("\r\n ", "").count("x" * 5000) == 1       # the description in full, not the 4000 the metadata keeps
    assert cal.external.box._db.execute("SELECT action_type FROM request WHERE id = ?", (item,)).fetchone()[0] == "calendar.invite"


def test_a_guest_the_lists_no_longer_allow_ends_not_allowed_and_nothing_is_written(env):
    """protects=the lists are checked again at approval (C3); fails_when=it is written, or it waits again; no_claim=none."""
    cal, server = env
    item = invite(cal)
    cal.s = dataclasses.replace(cal.s, invite_allowlist=("ben@example.org",))
    with pytest.raises(Rechecked) as e:
        approve(cal, item)
    assert e.value.reason == "not_allowed" and state(cal, item) == ("discarded", "not_allowed") and writes(server) == []


@pytest.mark.parametrize("lists, guest", [((), "anyone@example.net"), (("@club.example.org",), "kim@club.example.org")])
def test_the_lists_at_approval_are_the_calendars_own(env, lists, guest):
    """protects=Codex's finding 4: an empty INVITE_ALLOWLIST allows everyone and a domain entry the domain, at approval as when
    asked; fails_when=the gate's exact matching is used; no_claim=none."""
    cal, server = env
    cal.s = dataclasses.replace(cal.s, invite_allowlist=lists)
    item = invite(cal, attendees=[guest])
    assert approve(cal, item)["status"] == "sent" and len(writes(server)) == 1


def test_a_reply_checks_the_organizer(env):
    """protects=a reply reaches the organizer, so the list is checked for the organizer at approval; fails_when=it is
    written to an organizer the list no longer allows; no_claim=none."""
    cal, server = env
    server.events["home"]["inv.ics"] = (EVENT.format(uid="inv", org="lead@club.example.org").replace(
        "mailto:jana@example.org", f"mailto:{ME}"), '"e901"')
    r = cal.rsvp("inv", "accepted")
    assert r["status"] == "queued_for_owner_approval"
    cal.s = dataclasses.replace(cal.s, invite_allowlist=("jana@example.org",))
    with pytest.raises(Rechecked):
        approve(cal, r["outbox_id"])
    assert state(cal, r["outbox_id"]) == ("discarded", "not_allowed") and writes(server) == []


@pytest.mark.parametrize("change", ["edited", "gone"])
def test_an_event_changed_or_gone_at_the_read_ends_target_changed(env, change):
    """protects=the read just before the write (C2, D5-3); fails_when=it is written over the other change, or waits forever;
    no_claim=none."""
    cal, server = env
    item = seeded_update(cal, server)
    if change == "edited":
        server.events["home"]["bc1.ics"] = (server.events["home"]["bc1.ics"][0], '"e999"')
    else:
        del server.events["home"]["bc1.ics"]
    with pytest.raises(Rechecked) as e:
        approve(cal, item)
    assert e.value.reason == "target_changed" and state(cal, item) == ("discarded", "target_changed") and writes(server) == []


def test_deleting_an_event_that_is_already_gone_says_so(env):
    """protects=D5-3: a delete of a gone event is not reported as done; fails_when=it ends sent; no_claim=none."""
    cal, server = env
    server.events["home"]["bc1.ics"] = (EVENT.format(uid="bc1", org=ME), '"e900"')
    r = cal.delete_event("bc1")
    assert r["status"] == "queued_for_owner_approval"
    del server.events["home"]["bc1.ics"]
    with pytest.raises(Rechecked):
        approve(cal, r["outbox_id"])
    assert state(cal, r["outbox_id"]) == ("discarded", "target_changed")


def test_an_invitation_whose_id_is_taken_by_now_ends_target_changed(env):
    cal, server = env
    item = invite(cal)
    name = f"{A.read(cal.external.box.pending()[0].raw)[1]['uid']}.ics"
    server.events["home"][name] = ("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", '"e5"')
    with pytest.raises(Rechecked):
        approve(cal, item)
    assert state(cal, item) == ("discarded", "target_changed") and writes(server) == []


def test_a_failed_read_keeps_it_waiting(env):
    """protects=a read that fails proves nothing was written; fails_when=it ends, or a write follows; no_claim=none."""
    cal, server = env
    item = invite(cal)
    real = server.__call__

    def down(method, url, **kw):
        if method == "GET":
            raise niquests.exceptions.ReadTimeout("slow")
        return real(method, url, **kw)
    server.__class__ = type("Down", (caldav_fakes.Server,), {"__call__": lambda self, m, u, **kw: down(m, u, **kw)})
    with pytest.raises(CalendarError, match="still waits"):
        approve(cal, item)
    assert state(cal, item) == ("waiting", None) and writes(server) == []


class Faulty(caldav_fakes.Server):
    """Answers the write itself with `fault`: an exception to raise, or a status to return, after the GET went through."""
    fault = None
    before = None

    def __call__(self, method, url, data=None, headers=None, auth=None, **kw):
        if method in ("PUT", "DELETE"):
            if self.before:
                self.before(self)
            if isinstance(self.fault, BaseException):
                self.log.append((method, "fault", url))
                raise self.fault
            if isinstance(self.fault, int):
                self.log.append((method, "fault", url))
                return caldav_fakes.HTTPReply(self.fault, reason="Fault")
        return super().__call__(method, url, data=data, headers=headers, auth=auth, **kw)


def faulty(server, **kw):
    server.__class__ = Faulty
    for k, v in kw.items():
        setattr(server, k, v)


def test_a_write_that_never_left_keeps_it_waiting(env):
    """protects=only a connection that was never opened proves the write never left; fails_when=it ends unclear or sent;
    no_claim=the exception classes a real network raises (niquests builds them the same way)."""
    from urllib3.exceptions import NewConnectionError
    cal, server = env
    item = invite(cal)
    err = niquests.exceptions.ConnectionError(NewConnectionError(None, "refused"))
    faulty(server, fault=err)
    with pytest.raises(CalendarError, match="still waits"):
        approve(cal, item)
    assert state(cal, item) == ("waiting", None)
    faulty(server, fault=niquests.exceptions.ConnectTimeout("connect"))
    with pytest.raises(CalendarError):
        approve(cal, item)
    assert state(cal, item) == ("waiting", None)


@pytest.mark.parametrize("fault", [niquests.exceptions.ReadTimeout("read"), niquests.exceptions.ConnectionError("reset"), 500, 503])
def test_a_write_that_may_have_reached_icloud_ends_unclear_and_is_never_repeated(env, fault):
    """protects=D5-2 and C1: a timeout or reset after the write left, or a 5xx, is never written again; fails_when=it waits again,
    so a second approval could email the guests twice; no_claim=none."""
    cal, server = env
    item = invite(cal)
    faulty(server, fault=fault)
    r = approve(cal, item)
    assert r["status"] == "unclear" and state(cal, item) == ("unclear", None)
    assert cal.external.box.pending() == [] and len(writes(server)) == 1


def test_another_4xx_keeps_it_waiting(env):
    cal, server = env
    item = invite(cal)
    faulty(server, fault=409)
    with pytest.raises(CalendarError, match="HTTP 409"):
        approve(cal, item)
    assert state(cal, item) == ("waiting", None)


def test_a_change_between_the_read_and_the_write_waits_once_then_ends_target_changed(env):
    """protects=D5-6 and Codex's finding 1: iCloud's 412 to the write keeps the other writer's version, the request waits, and the
    next approval ends it at the read; fails_when=it is written over the change, ends sent, or waits forever; no_claim=none."""
    cal, server = env
    item = seeded_update(cal, server)

    def other_writer(srv):
        srv.events["home"]["bc1.ics"] = ("BEGIN:VCALENDAR\r\nX-OTHER:1\r\nEND:VCALENDAR\r\n", '"e777"')
        srv.before = None
    faulty(server, before=other_writer)
    with pytest.raises(CalendarError, match="changed in the calendar a moment ago"):
        approve(cal, item)
    assert state(cal, item) == ("waiting", None) and "X-OTHER" in server.events["home"]["bc1.ics"][0]
    with pytest.raises(Rechecked):
        approve(cal, item)
    assert state(cal, item) == ("discarded", "target_changed") and "X-OTHER" in server.events["home"]["bc1.ics"][0]


def test_an_update_is_written_with_the_version_it_was_based_on(env):
    cal, server = env
    item = seeded_update(cal, server)
    assert approve(cal, item)["status"] == "sent"
    assert "new room" in server.events["home"]["bc1.ics"][0] and len(writes(server)) == 1


def test_a_version_the_owner_did_not_look_at_is_not_written(env):
    cal, server = env
    item = invite(cal)
    with pytest.raises(CalendarError, match="changed just now"):          # the gate's words since 5b, for every type
        approve(cal, item, "0" * 64)
    assert state(cal, item) == ("waiting", None) and writes(server) == []


def test_a_calendar_change_cut_off_while_written_ends_unclear_at_the_next_start(tmp_path):
    """protects=package 5a criterion 4: a row of source calendar left executing ends unclear once, also when another source's
    store opened first, and is logged once; fails_when=it stays executing or waits again (it may have been written);
    no_claim=a real process kill: the first store is a second process that exits without finishing."""
    import subprocess
    import sys
    from pathlib import Path

    from icloud_mcp.audit import AuditLog
    from icloud_mcp.gate.status import ApprovalLedger
    src = Path(__file__).resolve().parents[1] / "src"
    code = ("import os, sys; sys.path.insert(0, %r)\n" % str(src)
            + "from icloud_mcp.gate.store import Store\n"
            + "st = Store(%r, 'calendar', 3600, 20)\n" % str(tmp_path)
            + "q = st.add(b'BEGIN:VCALENDAR', ['jana@example.org'], {'op': 'create'}, action_type='calendar.invite')\n"
            + "st.claim(q.id, 'executing')\nprint(q.id, flush=True)\nos._exit(0)\n")
    qid = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True).stdout.strip()
    Store(str(tmp_path), "external", 3600, 20)                        # another source's store takes the lock first
    box = Store(str(tmp_path), "calendar", 3600, 20)
    audit = AuditLog(str(tmp_path))
    ApprovalLedger({"calendar": box}, audit)
    assert box.state_of(qid) == ("unclear", "interrupted") and box.pending() == []
    lines = [e for e in audit.entries(10) if e["action"] == "approval.sent"]
    assert len(lines) == 1 and lines[0]["details"]["kind"] == "calendar" and lines[0]["details"]["delivery"] == "unconfirmed"


CAL_META = {"calendar_url": "https://caldav.example.com/cal/", "calendar": "Club", "uid": "u1@example.org", "summary": "Autumn fair",
            "start": "2026-12-05T10:00:00+00:00", "end": "2026-12-05T12:00:00+00:00", "location": "Example Street 1",
            "description": "Bring a cake", "organizer": None, "href": None, "etag": None}
OBJ = EVENT.format(uid="u1@example.org", org=ME).encode()


def test_the_server_moves_held_calendar_changes_to_the_calendar_types_once(tmp_path, monkeypatch):
    """protects=package 5b criterion 3, and 5a criterion 3 through the real wiring: changes held before 5b (the JSON file of before
    5a, and rows of source calendar, waiting and ended, one with two versions) wait on under external with their ids, states
    and versions, each with its type from the row and a new hash, and one approval.migrated line per waiting one naming both
    hashes; a second start moves nothing; fails_when=a change is lost, doubled, keeps its old hash or source, takes another
    type, or the log line is missing or repeated; no_claim=the Docker volume (same DATA_DIR rules)."""
    import hashlib
    import json
    from icloud_mcp.outbox import Outbox
    from icloud_mcp.server import create_server
    for k, v in dict(ICLOUD_USERNAME=ME, ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16, ALLOW_CALENDAR_INVITES="approve").items():
        monkeypatch.setenv(k, v)
    old = Outbox(str(tmp_path), 3600, 20, filename="calendar-outbox.json")
    a = old.add(OBJ, ["jana@example.org"], {**CAL_META, "op": "create"})
    rows = Store(str(tmp_path), "calendar", 3600, 20)
    upd = {**CAL_META, "op": "update", "href": "https://caldav.example.com/cal/u1.ics", "etag": '"e900"'}
    b = rows.add(OBJ, ["jana@example.org"], upd, action_type="calendar.update")
    b2 = rows.replace(b.id, b.sha256, OBJ.replace(b"Book club", b"Book club, new room"), followup=upd)
    gone = rows.add(OBJ, ["tom@example.net"], {**upd, "op": "delete"}, action_type="calendar.delete")
    rows.claim(gone.id, "discarded")
    mcp, _ = create_server(Settings.from_env())
    box = mcp._icloud_outboxes["external"]
    svc = mcp._icloud_dashboard.sources["external"].registry.get("calendar.invite").executor.svc
    want_a = svc.convert(OBJ, {"followup": {**CAL_META, "op": "create"}, "recipients": ["jana@example.org"], "action_type": "calendar.invite"})
    want_b = svc.convert(b2.raw, {"followup": upd, "recipients": ["jana@example.org"], "action_type": "calendar.update"})
    got = {q.id: (q.sha256, q.action_type) for q in box.pending()}
    assert got == {a.id: (hashlib.sha256(want_a).hexdigest(), "calendar.invite"), b.id: (hashlib.sha256(want_b).hexdigest(), "calendar.update")}
    assert [v["author"] for v in box.versions(b.id)] == ["assistant", "owner"] and box.versions(b.id)[-1]["raw"] == want_b
    assert box.state_of(gone.id) == ("discarded", None) and not box.has_source("calendar") and "calendar" not in mcp._icloud_outboxes
    assert box._db.execute("SELECT action_type FROM request WHERE id = ?", (gone.id,)).fetchone()[0] == "calendar.delete"
    assert (tmp_path / "calendar-outbox.json.migrated").exists() and not (tmp_path / "calendar-outbox.json").exists()
    lines = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines() if "approval.migrated" in x]
    assert sorted((e["details"]["outbox_id"], e["details"]["old_sha256"], e["details"]["sha256"], e["details"]["action_type"]) for e in lines) == \
           sorted([(a.id, a.sha256, got[a.id][0], "calendar.invite"), (b.id, b2.sha256, got[b.id][0], "calendar.update")])
    create_server(Settings.from_env())
    assert len([x for x in (tmp_path / "audit.jsonl").read_text().splitlines() if "approval.migrated" in x]) == 2


def test_the_five_calendar_types_are_the_servers_own_with_the_badge_button_and_tool_of_each(env):
    """protects=package 5b, D5-4: one type per operation with its fixed badge and main button, the tool that asks for it (so
    approval_request refuses it), the calendar's own lists (the gate's list lets everyone through) and a subject in place of a
    title; fails_when=a type is missing, takes the event's name as its title, or can be asked for with approval_request;
    no_claim=how the card looks (tests/test_dashboard_cards.py)."""
    cal, _ = env
    got = {r.action_type.id: (r.action_type.title, r.action_type.badge, r.action_type.approve_label, r.channel_tool, r.builtin,
                              r.allowed_set()) for r in cal.external.registry.registrations()}
    assert got == {"calendar.invite": ("Invitation", "Invitation", "Add and invite", "calendar_create_event", True, {"*"}),
                   "calendar.update": ("Event update", "Event update", "Save and notify", "calendar_update_event", True, {"*"}),
                   "calendar.delete": ("Cancellation", "Cancellation", "Delete and notify", "calendar_delete_event", True, {"*"}),
                   "calendar.cancel": ("Cancellation", "Cancellation", "Cancel and notify", "calendar_delete_event", True, {"*"}),
                   "calendar.reply": ("Reply", "Reply", "Send answer", "calendar_respond_to_event", True, {"*"})}
    item = invite(cal)
    v = cal.external.describe(next(q for q in cal.external.box.pending() if q.id == item))
    assert (v["title"], v["subject"], v["label"]) == ("Invitation", "Autumn fair", "jana@example.org")
