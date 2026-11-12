"""Accounts from the /setup page (accounts.py): settings per area, read-only per area, visible calendars."""
from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

from icloud_mcp import accounts
from icloud_mcp.cal import CalendarService
from icloud_mcp.config import Settings
from icloud_mcp.server import create_server


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16).items():
        monkeypatch.setenv(k, v)
    for k in ("READ_ONLY", "ALLOW_SEND", "IMAP_HOST", "CALDAV_URL", "ENABLE_MAIL"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _save(tmp_path, accts, visible=()):
    accounts.save(str(tmp_path), {"accounts": accts, "calendars_visible": list(visible)})


def test_without_the_file_nothing_changes(env):
    s = Settings.from_env()
    assert s.username == "me@icloud.com" and s.imap_host == "imap.mail.me.com" and not s.accounts_file
    assert s.mail_secret == s.caldav_secret == s.carddav_secret == "aaaa-bbbb-cccc-dddd"


def test_each_area_signs_in_with_its_own_account(env):
    _save(env, [{"id": "a", "provider": "webde", "username": "her@example.net", "password": "web-pw", "areas": ["mail"],
                 "display_name": "Her"},
                {"id": "b", "provider": "icloud", "username": "her@example.com", "password": "bbbb-cccc-dddd-eeee",
                 "areas": ["calendar", "contacts"], "read_only": True}], ["https://p01-caldav.icloud.com/1/calendars/home/"])
    s = Settings.from_env()
    assert s.accounts_file and (s.imap_host, s.imap_port, s.imap_security) == ("imap.web.de", 993, "ssl")
    assert (s.smtp_host, s.smtp_port, s.smtp_security) == ("smtp.web.de", 587, "starttls")
    assert s.imap_username == s.email_address == "her@example.net" and s.mail_secret == "web-pw" and s.display_name == "Her"
    assert s.caldav_username == "her@example.com" and s.caldav_secret == s.carddav_secret == "bbbb-cccc-dddd-eeee"
    assert s.caldav_url == "https://caldav.icloud.com" and s.read_only_areas == ("calendar", "contacts")
    assert s.writable("mail") and not s.writable("calendar") and s.calendars_visible == ("https://p01-caldav.icloud.com/1/calendars/home",)


def test_read_only_per_area_removes_only_that_areas_write_tools(env):
    _save(env, [{"id": "b", "provider": "icloud", "username": "her@example.com", "password": "bbbb-cccc-dddd-eeee",
                 "areas": ["mail", "calendar"], "read_only": False}])
    s = Settings.from_env()
    names = {t.name for t in asyncio.run(create_server(dataclasses.replace(s, read_only_areas=("calendar",)))[0].list_tools())}
    assert "mail_send_message" in names and "calendar_list_events" in names
    assert "calendar_create_event" not in names and not any(n.startswith("contacts_") for n in names)


def test_passwords_are_normalised_and_other_servers_decide_the_areas(env):
    assert accounts.normalise_password("icloud", "ABCD-efgh ijkl-mnop")[0] == "abcd-efgh-ijkl-mnop"
    assert accounts.normalise_password("icloud", "abcd-efgh")[1]
    assert accounts.normalise_password("other", "  Ein Pass 1  ") == ("Ein Pass 1", None)
    assert accounts.normalise_password("other", "")[1] == "Enter the password."
    other = {"provider": "other", "imap": "imap.example.org", "smtp": "smtp.example.org:465", "caldav": "https://dav.example.org/"}
    assert accounts.possible_areas(other) == ("mail", "calendar")
    t = accounts.trial_settings(Settings.from_env(), dict(other, username="x@example.org", password="p"))
    assert (t.smtp_port, t.smtp_security, t.imap_port, t.imap_security) == (465, "ssl", 993, "ssl")


def test_only_icloud_and_other_are_offered():
    assert set(accounts.PROVIDERS) == {"icloud", "other"} and accounts.PROVIDERS["other"]["label"] == "Other email & calendar"


@pytest.mark.parametrize("old, servers, areas", [
    ("webde", ("imap.web.de:993", "smtp.web.de:587", "https://caldav.web.de", "https://carddav.web.de"), ("mail", "calendar", "contacts")),
    ("gmail", ("imap.gmail.com:993", "smtp.gmail.com:465", "", ""), ("mail",))])
def test_a_saved_webde_or_gmail_account_loads_as_other_with_its_servers(env, old, servers, areas):
    """Files saved while /setup still offered WEB.DE and Gmail keep working: the account reads as "other" with the servers the
    preset had, so the server signs in where it always did, and a later save writes them out."""
    (env / accounts.ACCOUNTS_FILE).write_text(json.dumps({"accounts": [
        {"id": "x1", "provider": old, "username": "her@example.net", "password": "app-pass-1", "areas": ["mail"]}]}))
    acct = accounts.load(str(env))["accounts"][0]
    assert acct["provider"] == "other" and tuple(accounts.endpoints(acct).values()) == servers
    assert accounts.possible_areas(acct) == areas
    s = Settings.from_env()
    assert f"{s.imap_host}:{s.imap_port}" == servers[0] and f"{s.smtp_host}:{s.smtp_port}" == servers[1]
    accounts.save(str(env), accounts.load(str(env)))
    stored = json.loads((env / accounts.ACCOUNTS_FILE).read_text())["accounts"][0]
    assert stored["provider"] == "other" and (stored["imap"], stored["smtp"]) == servers[:2]


def test_an_account_with_no_area_stays_connected(env):
    """Turning off the last area of a service keeps it connected (Settings › its page › Used for); it serves nothing."""
    _save(env, [{"id": "a", "provider": "icloud", "username": "her@example.com", "password": "bbbb-cccc-dddd-eeee", "areas": ["mail"]},
                {"id": "b", "provider": "other", "username": "her@example.net", "password": "pw", "areas": ["contacts"],
                 "imap": "imap.example.net", "smtp": "smtp.example.net", "carddav": "https://dav.example.net/"}])
    data = accounts.put(accounts.load(str(env)), dict(accounts.load(str(env))["accounts"][0], areas=["mail", "contacts"]), "a")
    assert [(a["id"], a["areas"]) for a in data["accounts"]] == [("a", ["mail", "contacts"]), ("b", [])]
    accounts.save(str(env), data)
    assert [a["id"] for a in accounts.load(str(env))["accounts"]] == ["a", "b"] and accounts.load(str(env))["defaults"] == {"mail": "a", "contacts": "a"}


class _Cal:
    def __init__(self, url):
        self.url = url
        self.name = url.rsplit("/", 2)[-2]

    def get_supported_components(self):
        return ["VEVENT"]


class _Principal:
    def calendars(self):
        return [_Cal("https://p07-caldav.icloud.com/1/calendars/home/"), _Cal("https://p07-caldav.icloud.com/1/calendars/work/")]


def test_only_chosen_calendars_are_seen(env, monkeypatch):
    cals = _Principal().calendars()
    monkeypatch.setattr(CalendarService, "_calendar_objects", lambda self, principal: (cals, cals))   # the listing itself is upstream's
    s = dataclasses.replace(Settings.from_env(), calendars_visible=("https://p01-caldav.icloud.com/1/calendars/work",))
    assert [c.name for c in CalendarService(s)._event_calendars(_Principal())] == ["work"]
    assert len(CalendarService(dataclasses.replace(s, calendars_visible=()))._event_calendars(_Principal())) == 2
