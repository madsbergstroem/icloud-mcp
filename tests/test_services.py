"""Connected services in Settings (dashboard_accounts.py, setup_page.connect): the list, connecting iCloud or another provider, an
area moved between services, a new password, read only, disconnecting and the calendar choice. Every change needs the dashboard
session and, on the same request, the owner's Tailscale identity; it is checked with the provider (faked here) before it is
saved, and then the server restarts. Every address and password is made up."""
import json

import pytest

from icloud_mcp import accounts as A
from icloud_mcp import dashboard_accounts as DA
from test_dashboard import api, client, signed_in, web  # noqa: F401  (web is a fixture)

OWNER = {"Tailscale-User-Login": "owner@example.org"}         # what tailscale serve adds; the test client comes from 127.0.0.1
SSO = {"OWNER_TAILSCALE_LOGINS": "owner@example.org", "TRUSTED_PROXY_IPS": "127.0.0.1"}
CALS = [{"name": "Home", "id": "https://caldav.example.com/1/home/"}, {"name": "Work", "id": "https://caldav.example.com/1/work/"}]


@pytest.fixture
def checks(monkeypatch):
    """Sign-in checks faked: every account works unless its password is 'wrong' (refused) or bloc-kedb-lock-edzz (unreachable). The restart
    after a save is recorded, not run."""
    seen, restarts = [], []

    async def fake_check(_s, acct, areas=None):
        areas = list(areas if areas is not None else acct["areas"])
        seen.append((dict(acct), areas))
        bad = acct.get("password") in ("wrong", "bloc-kedb-lock-edzz", "wron-gwro-ngwr-ongg")
        kind = "unreachable" if acct.get("password") == "bloc-kedb-lock-edzz" else "refused"
        return {"failed": areas if bad else [], "kinds": {a: kind for a in areas} if bad else {}, "calendars": CALS}
    monkeypatch.setattr(A, "check", fake_check)
    monkeypatch.setattr(DA.threading, "Timer", lambda delay, fn: type("T", (), {"start": lambda self: restarts.append(delay)})())
    return seen, restarts


async def _hdr(c):
    """Signs in and returns the headers of a write from the app on the owner's own device."""
    return {**api(await signed_in(c)), **OWNER}


def _saved(tmp_path):
    return A.load(str(tmp_path))


# -- the list ---------------------------------------------------------------------------------------------------------------------
async def test_the_list_shows_each_service_and_never_a_password(web, tmp_path):
    app, *_ = web()
    async with client(app) as c:
        assert (await c.get("/dashboard/api/accounts")).status_code == 401
        await signed_in(c)
        r = (await c.get("/dashboard/api/accounts")).json()
        assert [(a["name"], a["address"], a["uses"], a["from_environment"]) for a in r["accounts"]] == \
            [("iCloud", "me@icloud.com", ["mail", "calendar", "contacts"], True)]
        assert r["connect"]["allowed"] is False and "Tailscale" in r["connect"]["why"]
        A.save(str(tmp_path), {"accounts": [
            {"id": "a1", "provider": "icloud", "username": "alex@example.org", "password": "abcd-efgh-ijkl-mnop", "areas": ["calendar", "mail"]},
            {"id": "a2", "provider": "webde", "username": "alex@example.net", "password": "secret-pass-word", "areas": ["contacts"], "read_only": True}],
            "calendars_visible": []})
        text = (await c.get("/dashboard/api/accounts")).text
        assert "secret-pass-word" not in text and "abcd-efgh" not in text and '"password"' not in text
        got = json.loads(text)["accounts"]
        assert [(a["name"], a["label"], a["uses"], a["read_only"], a["sign_in"]) for a in got] == \
            [("iCloud", "iCloud", ["mail", "calendar"], False, "App-specific password"),
             ("example.net", "Other email & calendar", ["contacts"], True, "Password")]
        assert got[1]["servers"]["carddav"] == "https://carddav.web.de" and got[0]["servers"] is None   # an old WEB.DE account
        assert got[1]["possible"] == ["mail", "calendar", "contacts"]


async def test_changing_needs_the_owners_tailscale_identity_on_the_request(web, checks, tmp_path):
    app, *_ = web(**SSO)
    async with client(app) as c:
        token = await signed_in(c)
        assert (await c.get("/dashboard/api/accounts")).json()["connect"] == {
            "allowed": False, "why": "Open the dashboard through Tailscale on your own device, so the server knows it is you.", "via": ""}
        assert (await c.get("/dashboard/api/accounts", headers=OWNER)).json()["connect"] == {"allowed": True, "why": "", "via": "Tailscale"}
        r = await c.post("/dashboard/api/accounts/env/change", headers=api(token), json={"areas": ["mail"]})
        assert r.status_code == 403 and "Tailscale" in r.json()["error"]
        r = await c.post("/dashboard/api/accounts/env/change", headers={**OWNER, "x-csrf": token}, json={"areas": ["mail"]})
        assert r.status_code == 403                                  # not from this page (no origin): nothing changes
    assert _saved(tmp_path) is None and checks[1] == []


async def test_without_tailscale_or_access_nothing_can_be_connected(web, checks, tmp_path):
    app, *_ = web()
    async with client(app) as c:
        hdr = await _hdr(c)
        r = await c.post("/dashboard/api/accounts/connect", headers=hdr, json={"provider": "icloud"})
    assert r.status_code == 403 and "OWNER_TAILSCALE_LOGINS" in r.json()["error"] and _saved(tmp_path) is None


# -- connecting -------------------------------------------------------------------------------------------------------------------
async def test_connecting_icloud_checks_saves_and_restarts(web, checks, tmp_path):
    seen, restarts = checks
    app, *_ = web(**SSO)
    (tmp_path / "settings.json").write_text(json.dumps({"ENABLE_MAIL": "false", "MAX_RECIPIENTS": "5"}))
    async with client(app) as c:
        hdr = await _hdr(c)
        r = await c.post("/dashboard/api/accounts/connect", headers=hdr, json={
            "provider": "icloud", "username": "her@example.com", "password": "ABCD efgh ijkl mnop", "areas": ["mail", "calendar"],
            "display_name": "Her Name"})
        assert r.status_code == 200, r.text
        assert r.json()["note"] == "Saved. The server restarts and is back in about half a minute." and "abcd" not in r.text
        listed = (await c.get("/dashboard/api/accounts")).json()["accounts"]
    assert restarts == [1.0] and seen[-1][1] == ["mail", "calendar"]
    saved = _saved(tmp_path)
    assert [(a["provider"], a["username"], a["password"], a["areas"], a.get("display_name")) for a in saved["accounts"]] == \
        [("icloud", "her@example.com", "abcd-efgh-ijkl-mnop", ["mail", "calendar"], "Her Name"),
         ("icloud", "me@icloud.com", "aaaa-bbbb-cccc-dddd", ["contacts"], "")]          # the deployment's account keeps the rest
    assert oct((tmp_path / A.ACCOUNTS_FILE).stat().st_mode & 0o777) == "0o600"
    # the accounts decide which areas are on: a switch from before is dropped, the rest of the settings stay
    assert json.loads((tmp_path / "settings.json").read_text()) == {"MAX_RECIPIENTS": "5"}
    assert [a["address"] for a in listed] == ["her@example.com", "me@icloud.com"]
    log = (tmp_path / "audit.jsonl").read_text() if (tmp_path / "audit.jsonl").exists() else ""
    assert "abcd-efgh" not in log


async def test_another_provider_takes_mail_and_icloud_keeps_the_rest(web, checks, tmp_path):
    app, *_ = web(**SSO)
    async with client(app) as c:
        hdr = await _hdr(c)
        bad = await c.post("/dashboard/api/accounts/connect", headers=hdr, json={
            "provider": "other", "imap": "imap.example.net", "smtp": "", "username": "her@example.net", "password": "pw", "areas": ["mail"]})
        assert bad.status_code == 400 and bad.json()["error"] == "Mail needs both servers: IMAP to receive and SMTP to send."
        r = await c.post("/dashboard/api/accounts/connect", headers=hdr, json={
            "provider": "other", "imap": "imap.example.net", "smtp": "smtp.example.net:465", "caldav": "", "carddav": "",
            "username": "her@example.net", "password": "Web-App-Pass1", "areas": ["mail", "calendar"]})
        assert r.status_code == 200, r.text
    saved = _saved(tmp_path)
    assert [(a["provider"], a["areas"]) for a in saved["accounts"]] == [("other", ["mail"]), ("icloud", ["calendar", "contacts"])]
    assert saved["accounts"][1]["id"] != "env" and A.problems(saved) == []          # the deployment's account got a real id
    env = A.env_overlay(str(tmp_path))
    assert (env["IMAP_HOST"], env["SMTP_PORT"], env["SMTP_SECURITY"], env["MAIL_PASSWORD"]) == ("imap.example.net", "465", "ssl", "Web-App-Pass1")
    assert env["CALDAV_USERNAME"] == "me@icloud.com" and env["CALDAV_PASSWORD"] == "aaaa-bbbb-cccc-dddd"


@pytest.mark.parametrize("password, status, head", [
    ("wron-gwro-ngwr-ongg", 400, "Apple did not accept the sign-in: the address and password do not match. Nothing was saved."),
    ("bloc-kedb-lock-edzz", 400, "Apple cannot be reached from this server right now (Mail). It is not your address or password, and nothing was saved."),
    ("abcd-efgh", 400, "An app-specific password from Apple has 16 letters, like abcd-efgh-ijkl-mnop.")])
async def test_a_failed_check_saves_nothing_and_says_why(web, checks, tmp_path, password, status, head):
    seen, restarts = checks
    app, *_ = web(**SSO)
    async with client(app) as c:
        hdr = await _hdr(c)
        r = await c.post("/dashboard/api/accounts/connect", headers=hdr, json={
            "provider": "icloud", "username": "her@example.net", "password": password, "areas": ["mail"]})
    assert r.status_code == status and r.json()["error"] == head and _saved(tmp_path) is None and restarts == []
    if password == "wron-gwro-ngwr-ongg":
        f = r.json()["failure"]
        assert f["entered"] == "her@example.net" and any("app-specific password" in x for x in f["reasons"])


# -- changing a service -----------------------------------------------------------------------------------------------------------
def _two(tmp_path):
    A.save(str(tmp_path), {"accounts": [
        {"id": "a1", "provider": "icloud", "username": "her@example.com", "password": "abcd-efgh-ijkl-mnop", "areas": ["mail", "calendar"]},
        {"id": "a2", "provider": "other", "username": "her@example.net", "password": "old-pass", "areas": ["contacts"], "read_only": True,
         "imap": "imap.example.net", "smtp": "smtp.example.net", "caldav": "", "carddav": "https://dav.example.net/"}],
        "calendars_visible": ["https://caldav.example.com/1/work/"]})


async def test_turning_an_area_on_moves_it_and_the_other_service_stays_connected(web, checks, tmp_path):
    seen, restarts = checks
    _two(tmp_path)
    app, *_ = web(**SSO)
    async with client(app) as c:
        hdr = await _hdr(c)
        r = await c.post("/dashboard/api/accounts/a1/change", headers=hdr, json={"areas": ["mail", "calendar", "contacts"]})
        assert r.status_code == 200, r.text
    assert seen == [(seen[0][0], ["contacts"])]                      # only what the change adds is checked
    saved = _saved(tmp_path)
    assert [(a["id"], a["areas"]) for a in saved["accounts"]] == [("a1", ["mail", "calendar", "contacts"]), ("a2", [])]
    assert saved["calendars_visible"] == ["https://caldav.example.com/1/work/"] and restarts == [1.0]


async def test_turning_off_the_last_area_keeps_the_service(web, checks, tmp_path):
    seen, _ = checks
    _two(tmp_path)
    app, *_ = web(**SSO)
    async with client(app) as c:
        hdr = await _hdr(c)
        r = await c.post("/dashboard/api/accounts/a2/change", headers=hdr, json={"areas": []})
        assert r.status_code == 200, r.text
        listed = (await c.get("/dashboard/api/accounts", headers=OWNER)).json()["accounts"]
    assert seen == [] and [(a["id"], a["uses"]) for a in listed] == [("a1", ["mail", "calendar"]), ("a2", [])]
    assert "contacts" not in _saved(tmp_path)["defaults"] and A.env_overlay(str(tmp_path))["ENABLE_CONTACTS"] == "false"


async def test_an_area_the_service_has_no_server_for_cannot_be_turned_on(web, checks, tmp_path):
    _two(tmp_path)
    app, *_ = web(**SSO)
    async with client(app) as c:
        hdr = await _hdr(c)
        r = await c.post("/dashboard/api/accounts/a2/change", headers=hdr, json={"areas": ["contacts", "calendar"]})
    assert r.status_code == 200 and _saved(tmp_path)["accounts"][1]["areas"] == ["contacts"]


async def test_a_new_password_is_checked_on_every_area_and_read_only_off_checks_sending(web, checks, tmp_path):
    seen, _ = checks
    _two(tmp_path)
    app, *_ = web(**SSO)
    async with client(app) as c:
        hdr = await _hdr(c)
        bad = await c.post("/dashboard/api/accounts/a1/change", headers=hdr, json={"password": "wron-gwro-ngwr-ongg"})
        assert bad.status_code == 400 and _saved(tmp_path)["accounts"][0]["password"] == "abcd-efgh-ijkl-mnop"
        ok = await c.post("/dashboard/api/accounts/a1/change", headers=hdr, json={"password": "zyxw vuts rqpo nmlk"})
        assert ok.status_code == 200
        assert _saved(tmp_path)["accounts"][0]["password"] == "zyxw-vuts-rqpo-nmlk" and seen[-1][1] == ["mail", "calendar"]
        seen.clear()
        await c.post("/dashboard/api/accounts/a2/change", headers=hdr, json={"areas": ["mail", "contacts"], "read_only": False})
    assert seen[-1][1] == ["mail"] and not _saved(tmp_path)["accounts"][1]["read_only"]


async def test_disconnecting_removes_the_account_and_its_password(web, checks, tmp_path):
    _two(tmp_path)
    app, *_ = web(**SSO)
    async with client(app) as c:
        hdr = await _hdr(c)
        r = await c.post("/dashboard/api/accounts/a1/remove", headers=hdr)
        missing = await c.post("/dashboard/api/accounts/zz/remove", headers=hdr)
    assert r.status_code == 200 and missing.status_code == 404
    saved = _saved(tmp_path)
    assert [a["id"] for a in saved["accounts"]] == ["a2"] and saved["calendars_visible"] == []
    assert "abcd-efgh-ijkl-mnop" not in (tmp_path / A.ACCOUNTS_FILE).read_text()


async def test_the_calendar_service_chooses_which_calendars_the_assistant_sees(web, checks, tmp_path):
    _two(tmp_path)
    app, *_ = web(**SSO)
    async with client(app) as c:
        hdr = await _hdr(c)
        got = (await c.get("/dashboard/api/accounts/a1/calendars", headers=OWNER)).json()
        assert [(x["name"], x["visible"]) for x in got["calendars"]] == [("Home", False), ("Work", True)] and got["why"] == ""
        assert (await c.get("/dashboard/api/accounts/a2/calendars")).status_code == 404
        none = await c.post("/dashboard/api/accounts/a1/calendars", headers=hdr, json={"visible": []})
        assert none.status_code == 400
        r = await c.post("/dashboard/api/accounts/a1/calendars", headers=hdr, json={"visible": [CALS[0]["id"], "javascript:x"]})
    assert r.status_code == 200 and _saved(tmp_path)["calendars_visible"] == [CALS[0]["id"]]
