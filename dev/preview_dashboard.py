"""Run the dashboard locally with made-up data, to look at a change: a fake mailbox, a fake Mac helper and a few waiting requests
(mail, iMessage, a group chat, a calendar invitation) and one request the assistant took back (Home says so for a day, Activity
keeps it). Nothing here reaches a real account.

    PYTHONPATH=src python dev/preview_dashboard.py [--port 8765] [--data DIR] [--arrive SECONDS] [--beeper ok|no-key|down|refused|none]
                                                   [--owner yes|no]

Open http://localhost:8765/dashboard and sign in with the owner password printed at start. Words in a message steer the fake Mac:
[unclear] ends as "not sure it arrived", [fail] as "not sent, still waiting". Mail waits as actions of the three mail types (a
reply with a file, a forward, one that waits for a file) and goes through their real recheck and executor against a made-up SMTP
server: [unclear] in a mail makes the server take it and then never answer (unclear), [offline] finds no server (it waits again),
and old.address@example.net is refused while the others are taken. --arrive queues one more request after that many
seconds, to watch a new request show up without a reload. With --owner yes (the default) every request carries the owner's Tailscale
identity, as behind tailscale serve: the dashboard signs you in on the way. --owner no leaves it out, so you sign in with the
password and Settings shows why services cannot be connected or changed. --nothing-waits empties the queues, so Home shows
one column with what was taken back and what was sent today. "Find servers" finds made-up servers for an address at example.net
and nothing for any other. --gateway adds the connector file's example action (a marketplace reply, waiting) and a second action
whose list is empty (the listing is sold after the first look, so the first Send shows a new version), behind a made-up gateway whose agent key can call the send tool (bypass), cannot (clean), or is not set
(unchecked). --demo-plugins adds two made-up service plugins (Chatty with messages, Mailbox with mail), loaded
the way an installed plugin is. Account changes are checked against made-up servers (a password starting with "wron" is refused)
and saved to the preview's own folder; the restart that follows is skipped. Needs the test extras (pip install -e '.[test]')."""
import argparse
import asyncio
import contextlib
import dataclasses
import datetime
import json
import os
import sys
import tempfile
import threading
import time
from email.message import EmailMessage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8765)
ap.add_argument("--data", default="")
ap.add_argument("--arrive", type=int, default=0)
ap.add_argument("--beeper", choices=["ok", "no-key", "down", "refused", "none"], default="ok")
ap.add_argument("--owner", choices=["yes", "no"], default="yes")
ap.add_argument("--demo-plugins", action="store_true")
ap.add_argument("--nothing-waits", action="store_true")
ap.add_argument("--gateway", choices=["bypass", "clean", "unchecked", "none"], default="none")
args = ap.parse_args()
PORT = args.port
DATA = args.data or tempfile.mkdtemp(prefix="icloud-mcp-preview-")
PASSWORD = "preview-only-password-123"            # a made-up password for this local preview only
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
if os.path.isdir(DATA) and os.listdir(DATA):
    sys.exit(f"{DATA} is not empty. The preview writes its own made-up data there: give it an empty or new folder.")
os.makedirs(DATA, exist_ok=True)

os.environ.update(
    ICLOUD_USERNAME="alex.morgan@example.org", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", MCP_PUBLIC_URL=f"http://localhost:{PORT}",
    MCP_OWNER_PASSWORD=PASSWORD, DATA_DIR=DATA, ALLOW_SEND="true", WARMUP_ON_START="false",
    DEFAULT_TIMEZONE="Europe/Berlin", ENABLE_IMESSAGE="true", IMESSAGE_ALLOW_SEND="true", IMESSAGE_SEND_ALLOWLIST="*", INVITE_ALLOWLIST="@example.org,@example.net",
    BRIDGE_TOKEN="t" * 40, ALLOW_CALENDAR_INVITES="approve", ICLOUD_KEYCHAIN="false", OUTBOX_TTL_SECONDS="86400", OUTBOX_MAX="40",   # one store for every kind since 6b
    OWNER_TAILSCALE_LOGINS="alex.morgan@example.org", TRUSTED_PROXY_IPS="192.0.2.1")   # no request comes from that proxy: see --owner
# Beeper behind a made-up gateway (tests/test_beeper.py): networks in Accounts, a waiting Beeper message. --beeper picks how the
# gateway answers Accounts: ok, no-key (no read key), down (not reachable), refused (wrong key), none (no Beeper at all).
if args.beeper != "none":
    from test_beeper import KEY as BEEPER_KEY, READ_KEY as BEEPER_READ, URL as BEEPER_URL  # noqa: E402  (the made-up keys of the tests)
    os.environ.update(BEEPER_GATEWAY_URL=BEEPER_URL, BEEPER_EXEC_KEY=BEEPER_KEY, BEEPER_SEND_ALLOWLIST="!single:beeper.test,!gone:beeper.test",
                      **({} if args.beeper == "no-key" else {"BEEPER_READ_KEY": BEEPER_READ[::-1] if args.beeper == "refused" else BEEPER_READ}))

if args.demo_plugins:                     # two made-up service plugins from tests/test_plugins.py, found as entry points would be
    from icloud_mcp import plugins as _P  # noqa: E402
    _P.entry_points = lambda: [("chatty", "test_plugins:chatty_plugin"), ("mailbox", "test_plugins:mailbox_plugin")]
    os.environ["CHATTY_SEND_ALLOWLIST"] = "room-1"

if args.gateway != "none":                 # the connector file's example behind a made-up gateway (dev/fake_gateway.py)
    import copy as _copy
    from fake_gateway import FakeGateway as _FakeGW  # noqa: E402
    from icloud_mcp.gate_connectors import actions as _G  # noqa: E402
    _EXEC, _AGENT = "exec-key-made-up-preview", "agent-key-made-up-preview"
    _grants = {_EXEC: {"market-call_operation", "market-call_write_operation"},
               _AGENT: {"market-call_operation"} | ({"market-call_write_operation"} if args.gateway == "bypass" else set())}
    _gw = _FakeGW(_grants)

    _asked = {"n": 0}

    @_gw.tool("market-call_operation")
    def _read(site: str, op: str, args: dict) -> dict:              # the listing is sold after the first look: Send makes version 2
        _asked["n"] += 1
        title = "Bike trailer, two seats" if _asked["n"] == 1 else "Bike trailer, sold"
        return {"data": {"title": title if args.get("id") == "c-1" else "Garden chairs"}}

    @_gw.tool("market-call_write_operation")
    def _write(site: str, op: str, args: dict, idempotencyKey: str = "") -> dict:
        return {"write": "applied"}
    _doc = json.load(open(os.path.join(ROOT, "docs", "examples", "actions.example.json")))
    _doc["actions"][0]["rows"].append(["Chat", "conversation_id"])           # package 2d: the owner may change the chat as well
    _doc["actions"][0]["editable_fields"] = ["offer", "conversation_id"]
    _offer = _copy.deepcopy(_doc["actions"][0])
    _offer.update(action="market.offer.send", title="Marketplace offer", allow_setting="MARKET_OFFER_ALLOWLIST")
    _doc["actions"].append(_offer)
    _file = os.path.join(tempfile.mkdtemp(prefix="icloud-mcp-preview-gw-"), "actions.json")
    json.dump(_doc, open(_file, "w"))
    os.environ.update(GATE_ACTIONS_FILE=_file, GATE_GATEWAY_URL="http://gateway.test/mcp", GATE_EXEC_KEY=_EXEC, MARKET_SEND_ALLOWLIST="c-1",
                      **({} if args.gateway == "unchecked" else {"GATE_AGENT_KEY_PROBE": _AGENT}))
    _Real = _G.Gateway
    _G.Gateway = lambda url, key, timeout, make_http=None: _Real(url, key, timeout, make_http=_gw.make_http)

import uvicorn  # noqa: E402

from icloud_mcp import accounts as A  # noqa: E402
from icloud_mcp.cal import CalendarError, CalendarService  # noqa: E402
from icloud_mcp.config import Settings  # noqa: E402

from icloud_mcp.mail import MailService  # noqa: E402
from icloud_mcp.server import build_app, create_server  # noqa: E402
from test_outbox import FakeIMAP  # noqa: E402

# -- mail: a made-up SMTP server, steered by a word in the mail ---------------------------------------------------------------------
# The real recheck and executor of the mail types and the stepwise send run (packages 6a and 6b; every ending is a state of the
# approval store); only the SMTP server is made up (tests/smtp_fakes.py). [unclear]: it takes the message and never answers, so the read times out after DATA. [offline]:
# no server answers at all, so the mail waits again. old.address@example.net is refused at RCPT while the others are taken.
import smtp_fakes  # noqa: E402

import icloud_mcp.mail as _mail_mod  # noqa: E402


class PreviewSMTP(smtp_fakes.Server):
    def _fault(self, step):
        if step == "after" and self.messages and b"[unclear]" in self.messages[-1]:
            return "hang"
        return None


smtp_server = PreviewSMTP()
smtp_server.refuse = {"old.address@example.net"}
smtp_server.hang_seconds = 8.0
_mail_mod.READ_TIMEOUT = 4.0                                  # the preview gives up on a silent server after four seconds
_nobody = smtp_fakes.Server()                                 # a port where nothing listens any more: [offline]
_offline_port = _nobody.port
_nobody.close()
os.environ.update(SMTP_HOST="127.0.0.1", SMTP_PORT=str(smtp_server.port), SMTP_SECURITY="none")

s = Settings.from_env()
fake = FakeIMAP()


@contextlib.contextmanager
def fake_imap(self):
    yield fake


MailService.imap = fake_imap
MailService.resolve_folder = lambda self, c, name: {"sent": "Sent Messages", "drafts": "Drafts"}.get(name.lower(), name)
_real_smtp_send = MailService._smtp_send


def _preview_smtp_send(self, msg, rcpts):
    time.sleep(1.2)                                          # long enough to see "Sending…"
    if "[offline]" not in str(msg["Subject"] or ""):
        return _real_smtp_send(self, msg, rcpts)
    self._smtp_drop(quit=False)
    kept, self.s = self.s, dataclasses.replace(self.s, smtp_port=_offline_port)
    try:
        return _real_smtp_send(self, msg, rcpts)
    finally:
        self.s = kept


MailService._smtp_send = _preview_smtp_send

from icloud_mcp import beeper as B  # noqa: E402
from test_beeper import FakeGateway  # noqa: E402

gw = FakeGateway()
gw.mode = "down" if args.beeper == "down" else None
B.connect = lambda s: B.Beeper(B.Gateway(s.beeper_gateway_url, s.beeper_exec_key, 5, make_http=gw.make_http), s.beeper_chat_tool, s.beeper_send_tool)
B.accounts_reader = lambda s: B.AccountsReader(s, make_http=gw.make_http)
B.chats_reader = lambda s: B.ChatsReader(s, make_http=gw.make_http)
B.read_gateway = lambda s, make_http=None: B.Gateway(s.beeper_gateway_url, s.beeper_read_key, 5, make_http=gw.make_http)
# more made-up chats to find and add: a person on Telegram and a group of three on Signal; "!gone" on the list is a chat Beeper lost
gw.chats.update({"!morgan:beeper.test": {"id": "!morgan:beeper.test", "title": "Morgan Lee", "type": "single", "network": "Telegram", "accountID": "telegram-example-1",
                                          "participants": {"items": [], "total": 2}}})

# The owner's Tailscale identity on every request (--owner yes), as tailscale serve would add it: the dashboard signs the owner in
# on the way, as it does behind tailscale serve
from icloud_mcp import access_sso  # noqa: E402


async def _owner(self, request):
    return ("alex.morgan@example.org", "Tailscale") if args.owner == "yes" else None


access_sso.OwnerSSO.owner = _owner


# Sign-in checks against made-up servers: every account works unless its password starts with "wron" (wrong, wron-gwro-ngwr-ongg)
async def _check(_s, acct, areas=None):
    bad = str(acct.get("password", "")).startswith("wron")
    areas = list(areas if areas is not None else acct["areas"])
    await asyncio.sleep(1.2)                                         # long enough to see "Checking…"
    return {"failed": areas if bad else [], "kinds": {a: "refused" for a in areas} if bad else {},
            "calendars": [{"name": "Home", "id": "https://caldav.example.com/1/home/"}, {"name": "Work", "id": "https://caldav.example.com/1/work/"},
                          {"name": "Family", "id": "https://caldav.example.com/1/family/"}]}


A.check = _check


# "Find servers" without the internet: an address at example.net finds made-up servers, any other finds nothing
from icloud_mcp import discover as D  # noqa: E402


async def _discover(address):
    if "@" not in address:
        raise ValueError("Enter a full email address first, like alex@example.org.")
    await asyncio.sleep(1.0)
    if not address.lower().endswith("example.net"):
        return {"found": False, "source": "", "servers": {"imap": "", "smtp": "", "caldav": "", "carddav": ""}, "username": address, "notes": []}
    return {"found": True, "source": "provider", "username": address, "notes": [],
            "servers": {"imap": "mail.example.net:993", "smtp": "mail.example.net:587", "caldav": "https://dav.example.net/", "carddav": ""}}


D.discover = _discover
mcp, provider = create_server(s)
d = mcp._icloud_dashboard
d.exit_fn = lambda: print("(preview) the server would restart now", flush=True)
ext = d.sources["external"]
mail = ext.registry.get("mail.send").executor.svc               # the server's own MailService (package 6b: three action types)
cal = ext.registry.get("calendar.invite").executor.svc         # the server's own CalendarService (package 5b: five action types)
from icloud_mcp.imessage import ACTION_ID as IMESSAGE, params_of  # noqa: E402
im_service = ext.registry.get(IMESSAGE).recheck.__self__        # the server's own IMessageService (package 4b: an action type)

# -- how a release goes: a made-up Mac, steered by a word in the text --------------------------------------------------------------
# The real recheck and executor of imessage.send run (package 4b; every ending is a state of the approval store); only the Mac is made up.
from icloud_mcp.bridge import BridgeError  # noqa: E402

GROUPS = {"chat900": {"chat_id": "chat900", "name": "Weekend hike", "group": True, "services": ["iMessage"],
                      "participants": ["anna@example.org", "tom@example.net"]},
          "chat901": {"chat_id": "chat901", "name": "Book club", "group": True, "services": ["iMessage"],   # Sam joined after it was queued
                      "participants": ["anna@example.org", "sam@example.net", "tom@example.net"]}}


class PreviewMac:
    def call(self, op, args=None):
        args = args or {}
        if op == "imessage_read":
            c = GROUPS.get(args.get("chat_id"))
            if c is None:
                raise BridgeError(f"no conversation with chat_id '{args.get('chat_id')}' (take it from imessage_list_chats)")
            return {"chat": c, "messages": [], "complete": True}
        if op == "imessage_send":
            time.sleep(1.6)                                          # long enough to see "Sending…"
            text = args.get("text", "")
            if "[unclear]" in text:
                return {"status": "unconfirmed"}
            if "[fail]" in text:
                raise BridgeError("The Mac did not pick up the request within 60s.")
            if "[lost]" in text:
                raise BridgeError("The Mac picked up the request but did not finish within 60s.")
            return {"status": "sent", "message_id": "m1", "delivered": True}
        raise BridgeError(f"the preview Mac does not do {op}")


im_service.bridge = PreviewMac()


# -- calendar changes: a made-up iCloud calendar server, steered by a word in the summary ---------------------------------------
# The real recheck and executor of the calendar types run (package 5b; every ending is a state of the approval store); only the
# CalDAV server is made up (tests/caldav_fakes.py, which honours If-Match). [unclear]: the write times out after it left. [gone]: the event is deleted
# in the calendar before the owner approves.
import caldav_fakes  # noqa: E402
import niquests  # noqa: E402

from icloud_mcp import cal as _cal_mod  # noqa: E402


class PreviewCalDAV(caldav_fakes.Server):
    def __call__(self, method, url, data=None, headers=None, auth=None, **kw):
        if method in ("PUT", "DELETE"):
            time.sleep(1.2)                                      # long enough to see "Sending…"
            if "[unclear]" in (data.decode() if isinstance(data, bytes) else (data or "")):
                raise niquests.exceptions.ReadTimeout("the calendar server did not answer in time")
        return super().__call__(method, url, data=data, headers=headers, auth=auth, **kw)


cal_server = PreviewCalDAV(password=s.app_password)


class _PreviewClient(_cal_mod.caldav.DAVClient):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.session.request = cal_server


_cal_mod.caldav.DAVClient = _PreviewClient

# -- saved accounts (what Connect a service keeps)----------------------------------------------------------------------------------
A.save(DATA, {"accounts": [
    {"id": "a1", "provider": "icloud", "username": "alex.morgan@example.org", "password": "abcd-efgh-ijkl-mnop", "areas": ["mail", "calendar"]},
    {"id": "a2", "provider": "other", "username": "alex@work.example.net", "password": "not-a-real-password", "areas": ["contacts"], "read_only": True,
     "imap": "mail.work.example.net:993", "smtp": "mail.work.example.net:587", "caldav": "https://dav.work.example.net/", "carddav": "https://dav.work.example.net/"}],
    "calendars_visible": []})

# -- waiting requests, all made up ------------------------------------------------------------------------------------------------
# The mail a reply answers and a forward passes on: read from the fake mailbox, as the card does when it is shown ("In reply to")
fake.original = (b"From: Accounts <accounts@example.org>\r\nTo: alex.morgan@example.org\r\nSubject: Invoice 2291\r\n"
                 b"Message-ID: <inv2291@example.org>\r\nDate: Mon, 16 Nov 2026 09:00:00 +0100\r\n\r\nPlease confirm the invoice for both deliveries.\r\n")
MailService._fetch_readable = lambda self, c, folder, uid, readonly=True, uidvalidity=None: (fake.original, (), None, 7, None)
MailService._fetch_raw = lambda self, c, folder, uid, readonly=True, uidvalidity=None: (fake.original, (), None, 7)
_pdf = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
mail.reply("INBOX", 7, "Hello,\n\nthank you for the invoice. The amount of 1,240.00 EUR covers both deliveries, so we release the payment "
                       "on Friday. The delivery note follows.\n\nBest regards,\nAlex", cc=["jana@example.org"],
           attachments=[{"filename": "invoice-2291.pdf", "content_base64": __import__("base64").b64encode(_pdf).decode()},
                        {"filename": "delivery-note.jpg", "content_base64": None}])         # one file present, one missing
mail.forward("INBOX", 7, ["sam@example.org"], note="Sam, for your records.")                 # a forward (mail.forward)
html = EmailMessage()
html["From"], html["To"], html["Subject"] = "alex.morgan@example.org", "newsletter@example.net", "Welcome aboard"
html.set_content("Welcome aboard! Here is what happens next.")
html.add_alternative("<p>Welcome aboard! Here is what happens next.</p>", subtype="html")
ext.queue_external("mail.send", mail.params_of(html, None), None)                           # a text that cannot be edited (HTML)
mail.send(to=["jana@example.org", "old.address@example.net"], subject="Book club minutes",
          body="Hi both,\n\nthe minutes of Thursday are below. Next meeting on the 4th.\n\nSam")
mail.send(to=["tom@example.net"], subject="Quarterly figures [unclear]", body="Hi Tom,\n\nthe figures for the quarter are attached as a table below.\n\nSam")
mail.send(to=["jana@example.org"], subject="Holiday rota [offline]", body="Hi Jana,\n\nhere is the rota for December.\n\nSam")


def imsg(text, chat="anna@example.org", name="", group=False, people=None):
    d = {"chat_id": chat if group else None, "handle": None if group else chat, "text": text,
         "to": {"name": name, "group": group, "participants": people or [{"handle": chat, "name": "Anna Rivera"}]}}
    return ext.queue_external(IMESSAGE, params_of(d), None)


imsg("Hi Anna, running ten minutes late. Start without me.")
imsg("Saturday works for me. I can drive, three seats free.", chat="chat900", name="Weekend hike", group=True,
     people=[{"handle": "anna@example.org", "name": "Anna Rivera"}, {"handle": "tom@example.net", "name": "Tom Berger"}])
imsg("Dinner on Sunday? [unclear]", chat="anna2@example.org", people=[{"handle": "anna2@example.org", "name": "Anna Rivera"}])
imsg("This one will not leave the Mac. [fail]", chat="anna3@example.org", people=[{"handle": "anna3@example.org", "name": "Anna Rivera"}])
imsg("Chapter four for Thursday?", chat="chat901", name="Book club", group=True,
     people=[{"handle": "anna@example.org", "name": "Anna Rivera"}, {"handle": "tom@example.net", "name": "Tom Berger"}])
imsg("Is the parcel with you? [lost]", chat="anna4@example.org", people=[{"handle": "anna4@example.org", "name": "Anna Rivera"}])

SEED = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//preview//EN\r\nBEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20261101T090000Z\r\n"
        "DTSTART:{start}\r\nDTEND:{end}\r\nSUMMARY:{summary}\r\nORGANIZER:mailto:{org}\r\n{guests}END:VEVENT\r\nEND:VCALENDAR\r\n")


def seed(uid, summary, org="alex.morgan@example.org", guests=("jana@example.org",), start="20261121T180000Z", end="20261121T193000Z"):
    lines = "".join(f"ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:{g}\r\n" for g in guests)
    cal_server.events["home"][f"{uid}.ics"] = (SEED.format(uid=uid, summary=summary, org=org, guests=lines, start=start, end=end), f'"seed-{uid}"')


cal.create_event(summary="Dinner with the Okafors", start="2026-11-14T19:00", end="2026-11-14T21:30", calendar="Calendar",
                 location="Harbour Street 4", attendees=["jana@example.org", "tom@example.net"],
                 description="Table for four at 19:00. Bring the wine list.")
seed("bookclub", "Book club")
cal.update_event("bookclub", summary="Book club, new room", location="Library, room 2")
seed("yoga", "Yoga class", guests=("kim@example.net",))
cal.delete_event("yoga")
del cal_server.events["home"]["yoga.ics"]                     # [gone]: deleted in the calendar before the owner approves
cal.create_event(summary="Board meeting [unclear]", start="2026-11-26T09:00", end="2026-11-26T10:00", calendar="Calendar",
                 attendees=["jana@example.org"])
seed("fair", "Autumn fair", org="lead@example.org", guests=("alex.morgan@example.org",))
cal.rsvp("fair", "accepted")
seed("choir", "Choir practice", guests=("sam@example.org",), start="20261203T180000Z", end="20261203T193000Z")
cal_server.events["home"]["choir.ics"] = (cal_server.events["home"]["choir.ics"][0].replace("SUMMARY:", "RRULE:FREQ=WEEKLY;COUNT=6\r\nSUMMARY:"),
                                          '"seed-choir"')
cal.delete_event("choir", occurrence_start="2026-12-10T18:00:00+00:00")   # one occurrence: the fifth type, calendar.cancel

if args.beeper in ("ok", "no-key", "refused"):              # a Beeper message to a made-up chat, through the real tool
    asyncio.run(mcp.call_tool("messages_send_request", {"chat_id": "beeper:!single:beeper.test", "text": "See you at the north gate at nine."}))
if args.gateway != "none":                                   # a marketplace reply, through the real tool
    asyncio.run(mcp.call_tool("approval_request", {"action": "market.message.send",
                                                   "params": {"conversation_id": "c-1", "offer": 35,
                                                              "text": "Is the trailer still free on Saturday?"}}))
if "external" in d.sources and d.sources["external"].registry.get("gate.allow") is not None:   # package 10: list entries on request
    def _allow(entry, reason):
        return asyncio.run(mcp.call_tool("approval_request", {"action": "gate.allow", "params": {
            "setting": "INVITE_ALLOWLIST", "entry": entry, "reason": reason}}))
    _allow("lena@example.com", "Lena asked to be invited to the book club.")           # approved already: "On request" in Settings
    _done = next(q for q in d.sources["external"].pending() if b'"gate.allow"' in q.raw)
    d.sources["external"].release(_done)
    _allow("tom@example.com", "Tom asked me to invite him to Saturday's hike.")    # waits on Home
if "external" in d.sources and d.sources["external"].registry.get("gate.trust") is not None:   # package 10: standing rules
    def _trust(type_id, person, reason, days=90):
        return asyncio.run(mcp.call_tool("approval_request", {"action": "gate.trust", "params": {
            "action_type": type_id, "recipient": person, "days": days, "reason": reason}}))
    _trust("calendar.invite", "jana@example.org", "Jana and I plan the book club together; she gets every date anyway.")
    _rule = next(q for q in d.sources["external"].pending() if b'"gate.trust"' in q.raw)
    d.sources["external"].release(_rule)                     # approved already: listed under Settings › Without approval
    d.sources["external"].standing.add("mail.send", "tom@example.net", 45)
    cal.create_event(summary="Lunch with Jana", start="2026-12-04T12:00", end="2026-12-04T13:00", calendar="Calendar",
                     attendees=["jana@example.org"])       # goes out at once: "Without approval" in Activity
    _trust("mail.send", "sam@example.org", "Sam and I write about the club's newsletter every week.")   # waits on Home
if "external" in d.sources:                                  # and a request whose type this server no longer has: "Other"
    from icloud_mcp import actions as AT
    d.sources["external"].box.add(AT.canonical("example.retired", {"note": "made up"}), ["nobody"], action_type="example.retired")

d.audit.record("signin.password", actor="unknown", via="password", ok=False, summary="Wrong owner password")
d.audit.record("signin.password", actor="owner", via="password", summary="Signed in with the owner password")
d.audit.record("settings.changed", actor="owner", via="password", summary="Changed safety settings: Quiet hours 22:00 → 23:00",
               keys=["QUIET_HOURS"], changes=[["Quiet hours", "22:00", "23:00"]])      # a change with its old and new value (Activity)
# One request the assistant queued and then took back, through the real tool, so Home and Activity show what a withdrawal leaves.
taken_back = mail.send(to=["jordan.lee@example.org"], subject="Offer for the delivery van", body="Hi Jordan, we can offer 18,400 EUR for the van.")["outbox_id"]
asyncio.run(mcp.call_tool("approval_withdraw", {"id": taken_back, "reason": "mistake"}))
if args.nothing_waits:                                       # Home with nothing waiting: the queues emptied, one mail sent today
    for src in d.sources.values():
        box = getattr(src, "box", None)
        for q in src.pending():
            box.claim(q.id)
    d.audit.record("approval.sent", actor="owner", via="password", summary="Approved email “Re: Invoice for October”",
                   kind="mail", recipients=["sam.taylor@example.org"])
if args.arrive:
    threading.Timer(args.arrive, lambda: imsg("Live arrival test: this appeared without a reload.", chat="live@example.org",
                                              people=[{"handle": "live@example.org", "name": "Lena Fischer"}])).start()
app = build_app(s, mcp)
print(f"preview on http://localhost:{PORT}/dashboard  (owner password: {PASSWORD})", flush=True)
uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
