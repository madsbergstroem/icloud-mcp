"""Run the dashboard locally with made-up data, to look at a change: a fake mailbox, a fake Mac helper and a few waiting requests
(mail, iMessage, a group chat, a calendar invitation). Nothing here reaches a real account.

    PYTHONPATH=src python dev/preview_dashboard.py [--port 8765] [--data DIR] [--arrive SECONDS]

Open http://localhost:8765/dashboard and sign in with the owner password printed at start. Words in a message steer the fake Mac:
[unclear] ends as "not sure it arrived", [fail] as "not sent, still waiting". --arrive queues one more request after that many
seconds, to watch a new request show up without a reload. Needs the test extras (pip install -e '.[test]')."""
import argparse
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
    BRIDGE_TOKEN="t" * 40, ALLOW_CALENDAR_INVITES="approve", ICLOUD_KEYCHAIN="false", OUTBOX_TTL_SECONDS="86400")

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

mcp, provider = create_server(s)
d = mcp._icloud_dashboard
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

# -- saved accounts (what /setup keeps) -------------------------------------------------------------------------------------------
A.save(DATA, {"accounts": [
    {"id": "a1", "provider": "icloud", "username": "alex.morgan@example.org", "password": "abcd-efgh-ijkl-mnop", "areas": ["mail", "calendar"]},
    {"id": "a2", "provider": "webde", "username": "alex.work@example.net", "password": "not-a-real-password", "areas": ["contacts"], "read_only": True}],
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

d.audit.record("signin.password", actor="owner", via="password", summary="Signed in with the owner password")
if args.arrive:
    threading.Timer(args.arrive, lambda: imsg("Live arrival test: this appeared without a reload.", chat="live@example.org",
                                              people=[{"handle": "live@example.org", "name": "Lena Fischer"}])).start()
app = build_app(s, mcp)
print(f"preview on http://localhost:{PORT}/dashboard  (owner password: {PASSWORD})", flush=True)
uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
