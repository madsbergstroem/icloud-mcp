"""Run the dashboard locally with made-up data, to look at a change: a fake mailbox, a fake Mac helper and a few waiting requests
(mail, iMessage, a group chat, a calendar invitation) and one request the assistant took back (Home says so for a day, Activity
keeps it). Nothing here reaches a real account.

    PYTHONPATH=src python dev/preview_dashboard.py [--port 8765] [--data DIR] [--arrive SECONDS] [--beeper ok|no-key|down|refused|none]
                                                   [--owner yes|no]

Open http://localhost:8765/dashboard and sign in with the owner password printed at start. Words in a message steer the fake Mac:
[unclear] ends as "not sure it arrived", [fail] as "not sent, still waiting". --arrive queues one more request after that many
seconds, to watch a new request show up without a reload. With --owner yes (the default) every request carries the owner's Tailscale
identity, as behind tailscale serve: the dashboard signs you in on the way. --owner no leaves it out, so you sign in with the
password and Settings shows why services cannot be connected or changed. --nothing-waits empties the queues, so Home shows
one column with what was taken back and what was sent today. "Find servers" finds made-up servers for an address at example.net
and nothing for any other. --demo-plugins adds two made-up service plugins (Chatty with messages, Mailbox with mail), loaded
the way an installed plugin is. Account changes are checked against made-up servers (a password starting with "wron" is refused)
and saved to the preview's own folder; the restart that follows is skipped. Needs the test extras (pip install -e '.[test]')."""
import argparse
import asyncio
import contextlib
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
    DEFAULT_TIMEZONE="Europe/Berlin", ENABLE_IMESSAGE="true", IMESSAGE_ALLOW_SEND="true", IMESSAGE_SEND_ALLOWLIST="*",
    BRIDGE_TOKEN="t" * 40, ALLOW_CALENDAR_INVITES="approve", ICLOUD_KEYCHAIN="false", OUTBOX_TTL_SECONDS="86400",
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

import uvicorn  # noqa: E402

from icloud_mcp import accounts as A  # noqa: E402
from icloud_mcp.cal import CalendarError, CalendarService  # noqa: E402
from icloud_mcp.config import Settings  # noqa: E402
from icloud_mcp.imessage import IMessageError, IMessageService  # noqa: E402
from icloud_mcp.mail import MailService  # noqa: E402
from icloud_mcp.server import build_app, create_server  # noqa: E402
from test_outbox import FakeIMAP  # noqa: E402

s = Settings.from_env()
fake, sent = FakeIMAP(), []


@contextlib.contextmanager
def fake_imap(self):
    yield fake


MailService.imap = fake_imap
MailService.resolve_folder = lambda self, c, name: {"sent": "Sent Messages", "drafts": "Drafts"}.get(name.lower(), name)
MailService._smtp_send = lambda self, msg, rcpts: sent.append((msg, list(rcpts))) or {}

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
mail = d.sources["mail"].mail
cal = d.sources["calendar"].cal
im = d.sources["imessage"]

# -- how a release goes, chosen by a word in the text ----------------------------------------------------------------------------
def release(self, box, item_id):
    q = box.claim(item_id)
    text = json.loads(q.raw)["text"]
    time.sleep(1.6)                                                  # long enough to see "Sending…"
    if "[unclear]" in text:
        return {"status": "unconfirmed", "to": "Hiking crew", "note": "Messages did not report it back."}
    if "[fail]" in text:
        box.restore(q)
        raise IMessageError("The Mac helper did not pick up. Nothing reached it.")
    return {"status": "sent", "to": "Hiking crew" if "chat900" in q.raw.decode() else "Anna Rivera", "note": ""}


IMessageService.release = release


def cal_release(self, q):
    time.sleep(1.0)
    return f"Added “{(q.followup or {}).get('summary')}”. iCloud now emails {', '.join(q.recipients)}."


CalendarService.release = cal_release

# -- saved accounts (what Connect a service keeps)----------------------------------------------------------------------------------
A.save(DATA, {"accounts": [
    {"id": "a1", "provider": "icloud", "username": "alex.morgan@example.org", "password": "abcd-efgh-ijkl-mnop", "areas": ["mail", "calendar"]},
    {"id": "a2", "provider": "other", "username": "alex@work.example.net", "password": "not-a-real-password", "areas": ["contacts"], "read_only": True,
     "imap": "mail.work.example.net:993", "smtp": "mail.work.example.net:587", "caldav": "https://dav.work.example.net/", "carddav": "https://dav.work.example.net/"}],
    "calendars_visible": []})

# -- waiting requests, all made up ------------------------------------------------------------------------------------------------
mail.send(to=["accounts@example.org"], subject="Re: Invoice 2291",
          body="Hello,\n\nthank you for the invoice. Please confirm that the amount of 1,240.00 EUR covers both deliveries. If so, we release the payment on Friday.\n\nBest regards,\nAlex")
html = EmailMessage()
html["From"], html["To"], html["Subject"] = "alex.morgan@example.org", "newsletter@example.net", "Welcome aboard"
html.set_content("Welcome aboard! Here is what happens next.")
html.add_alternative("<p>Welcome aboard! Here is what happens next.</p>", subtype="html")
mail.outbox.add(html.as_bytes(), ["newsletter@example.net"])


def imsg(text, chat="anna@example.org", name="", group=False, people=None):
    raw = json.dumps({"chat_id": chat, "handle": None if group else chat, "text": text,
                      "to": {"name": name, "group": group, "participants": people or [{"handle": chat, "name": "Anna Rivera"}]}}, ensure_ascii=False).encode()
    return im.box.add(raw, [chat])


imsg("Hi Anna, running ten minutes late. Start without me.")
imsg("Saturday works for me. I can drive, three seats free.", chat="chat900", name="Weekend hike", group=True,
     people=[{"handle": "anna@example.org", "name": "Anna Rivera"}, {"handle": "tom@example.net", "name": "Tom Berger"}])
imsg("Dinner on Sunday? [unclear]", chat="anna2@example.org", people=[{"handle": "anna2@example.org", "name": "Anna Rivera"}])
imsg("This one will not leave the Mac. [fail]", chat="anna3@example.org", people=[{"handle": "anna3@example.org", "name": "Anna Rivera"}])

from icalendar import Calendar, Event  # noqa: E402
c = Calendar(); c.add("prodid", "-//preview//"); c.add("version", "2.0")
ev = Event(); ev.add("uid", "preview-1@example.org"); ev.add("summary", "Dinner with the Okafors")
ev.add("dtstart", datetime.datetime(2026, 11, 14, 19, 0)); ev.add("description", "Table for four at 19:00. Bring the wine list.")
c.add_component(ev)
cal.outbox.add(c.to_ical(), ["jana@example.org", "tom@example.net"],
               {"op": "create", "calendar_url": "https://caldav.example.com/cal/", "calendar": "Home", "uid": "preview-1@example.org",
                "summary": "Dinner with the Okafors", "start": "2026-11-14T19:00:00+01:00", "end": None, "location": "Harbour Street 4",
                "description": "Table for four at 19:00. Bring the wine list.", "organizer": None, "href": None, "etag": None})

if args.beeper in ("ok", "no-key", "refused"):              # a Beeper message to a made-up chat, through the real tool
    asyncio.run(mcp.call_tool("messages_send_request", {"chat_id": "beeper:!single:beeper.test", "text": "See you at the north gate at nine."}))
if "external" in d.sources:                                  # and a request whose type this server no longer has: "Other"
    from icloud_mcp import actions as AT
    d.sources["external"].box.add(AT.canonical("example.retired", {"note": "made up"}), ["nobody"], action_type="example.retired")

d.audit.record("signin.password", actor="owner", via="password", summary="Signed in with the owner password")
# One request the assistant queued and then took back, through the real tool, so Home and Activity show what a withdrawal leaves.
taken_back = mail.send(to=["jordan.lee@example.org"], subject="Offer for the delivery van", body="Hi Jordan, we can offer 18,400 EUR for the van.")["outbox_id"]
asyncio.run(mcp.call_tool("approval_withdraw", {"id": taken_back, "reason": "mistake"}))
if args.nothing_waits:                                       # Home with nothing waiting: the queues emptied, one mail sent today
    for src in d.sources.values():
        box = getattr(getattr(src, "mail", None), "outbox", None) or getattr(getattr(src, "cal", None), "outbox", None) or getattr(src, "box", None)
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
