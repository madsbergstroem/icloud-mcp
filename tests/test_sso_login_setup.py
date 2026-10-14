"""Owner sign-in through Cloudflare Access on the /login consent page and the /setup page (access_sso.py)."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from icloud_mcp import access_sso, setup_page
from icloud_mcp.config import Settings
from icloud_mcp.server import create_server

from test_access_sso import AUD, OWNER, TEAM, FakeJWKS, token
from test_oauth_flow import BASE, PASSWORD, REDIRECT, client_for, pkce, register


def _server(tmp_path, monkeypatch, sso: bool = True, username: str = "me@icloud.com"):
    env = dict(ICLOUD_USERNAME=username, ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", MCP_PUBLIC_URL=BASE,
               MCP_OWNER_PASSWORD=PASSWORD, DATA_DIR=str(tmp_path))
    if sso:
        env.update(OWNER_ACCESS_TEAM_DOMAIN=TEAM, OWNER_ACCESS_AUD=AUD, OWNER_ACCESS_EMAILS=OWNER)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(access_sso, "PyJWKClient", FakeJWKS)
    mcp, provider = create_server(Settings.from_env())
    return mcp.streamable_http_app(host="0.0.0.0")


@pytest.fixture
def sso_app(tmp_path, monkeypatch):
    return _server(tmp_path, monkeypatch)


async def _pending(c):
    cid = (await register(c)).json()["client_id"]
    _verifier, challenge = pkce()
    r = await c.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
                                          "code_challenge": challenge, "code_challenge_method": "S256", "state": "s", "scope": "icloud"})
    return parse_qs(urlsplit(r.headers["location"]).query)["p"][0]


def _sso_field(page: str) -> str:
    return page.split('name="sso" value="', 1)[1].split('"', 1)[0]


async def test_login_with_access_identity_needs_no_password(sso_app):
    async with client_for(sso_app) as c:
        pid = await _pending(c)
        page = await c.get(f"/login?p={pid}", headers={"Cf-Access-Jwt-Assertion": token()})
        assert page.status_code == 200 and f"Signed in as <b>{OWNER}</b>" in page.text and "Owner password" not in page.text
        r = await c.post("/login", data={"p": pid, "sso": _sso_field(page.text), "action": "approve"},
                         headers={"Cf-Access-Jwt-Assertion": token()})
    assert r.status_code == 302 and r.headers["location"].startswith(REDIRECT) and "code=" in r.headers["location"]


async def test_login_sso_approval_needs_the_access_header_on_the_post(sso_app):
    async with client_for(sso_app) as c:
        pid = await _pending(c)
        page = await c.get(f"/login?p={pid}", headers={"Cf-Access-Jwt-Assertion": token()})
        r = await c.post("/login", data={"p": pid, "sso": _sso_field(page.text), "action": "approve"})
    assert r.status_code == 403 and "code=" not in r.headers.get("location", "")


async def test_login_sso_token_is_bound_to_its_request(sso_app):
    async with client_for(sso_app) as c:
        pid_a, pid_b = await _pending(c), await _pending(c)
        page_a = await c.get(f"/login?p={pid_a}", headers={"Cf-Access-Jwt-Assertion": token()})
        r = await c.post("/login", data={"p": pid_b, "sso": _sso_field(page_a.text), "action": "approve"},
                         headers={"Cf-Access-Jwt-Assertion": token()})
    assert r.status_code == 403


async def test_login_other_identity_gets_the_password_form(sso_app):
    async with client_for(sso_app) as c:
        pid = await _pending(c)
        page = await c.get(f"/login?p={pid}", headers={"Cf-Access-Jwt-Assertion": token(email="someone@example.org")})
    assert "Owner password" in page.text and 'name="sso"' not in page.text


async def test_password_still_works_with_sso_on(sso_app):
    async with client_for(sso_app) as c:
        pid = await _pending(c)
        r = await c.post("/login", data={"p": pid, "password": PASSWORD, "action": "approve"})
    assert r.status_code == 302 and "code=" in r.headers["location"]


async def test_setup_absent_without_sso(tmp_path, monkeypatch):
    app = _server(tmp_path, monkeypatch, sso=False)
    async with client_for(app) as c:
        r = await c.get("/setup", headers={"Cf-Access-Jwt-Assertion": token()})
    assert r.status_code == 404


async def test_setup_refuses_without_access_identity(sso_app):
    async with client_for(sso_app) as c:
        r = await c.get("/setup")
        p = await c.post("/setup", data={"password": "abcd-efgh-ijkl-mnop", "exp": "9999999999", "tok": "x"})
    assert r.status_code == 403 and p.status_code == 403


def _form_tokens(page: str) -> dict[str, str]:
    return {k: page.split(f'name="{k}" value="', 1)[1].split('"', 1)[0] for k in ("exp", "tok")}


@pytest.fixture
def pending_app(tmp_path, monkeypatch):
    return _server(tmp_path, monkeypatch, username="pending@setup.invalid")


@pytest.fixture
def setup_env(pending_app, monkeypatch):
    """The SSO app with sign-in checks faked: every account works unless its password is 'wrong'."""
    from icloud_mcp import accounts
    checked, timers = [], []

    async def fake_check(_s, acct, areas=None):
        checked.append(dict(acct))
        bad = acct.get("password") in ("wrong", "wron-gwro-ngwr-ongg")
        return {"failed": list(acct["areas"]) if bad else [],
                "calendars": [{"name": "Privat", "id": "https://caldav.example.com/1/home/"},
                              {"name": "Arbeit", "id": "https://caldav.example.com/1/work/"}]}
    monkeypatch.setattr(accounts, "check", fake_check)
    monkeypatch.setattr(setup_page.threading, "Timer", lambda delay, fn: type("T", (), {"start": lambda self: timers.append(delay)})())
    return pending_app, checked, timers


async def _connect(c, fields: dict, query: str = "anbieter=icloud&schritt=eintragen"):
    hdr = {"Cf-Access-Jwt-Assertion": token()}
    page = await c.get(f"/setup?{query}", headers=hdr)
    assert page.status_code == 200, page.text
    return await c.post("/setup", data={**_form_tokens(page.text), "action": "connect", **fields}, headers=hdr)


async def test_setup_wizard_saves_icloud_and_restarts(setup_env, tmp_path):
    from icloud_mcp import accounts
    app, checked, timers = setup_env
    hdr = {"Cf-Access-Jwt-Assertion": token()}
    async with client_for(app) as c:
        prep = await c.get("/setup?anbieter=icloud", headers=hdr)
        assert "support.apple.com/de-de/102654" in prep.text and "App-spezifische Passwörter" in prep.text
        r = await _connect(c, {"anbieter": "icloud", "konto": "", "username": "her@icloud.com", "password": "abcd efgh ijkl mnop",
                               "area": ["calendar", "contacts"], "display_name": "Her Name"})
    assert r.status_code == 200 and "Verbunden." in r.text and "abcd" not in r.text and timers == [1.0]
    saved = accounts.load(str(tmp_path))
    assert [(a["provider"], a["username"], a["password"], a["areas"]) for a in saved["accounts"]] == \
        [("icloud", "her@icloud.com", "abcd-efgh-ijkl-mnop", ["calendar", "contacts"])]
    assert oct((tmp_path / accounts.ACCOUNTS_FILE).stat().st_mode & 0o777) == "0o600"


async def test_setup_mail_from_webde_next_to_icloud(setup_env, tmp_path):
    from icloud_mcp import accounts
    app, _checked, _timers = setup_env
    async with client_for(app) as c:
        await _connect(c, {"anbieter": "icloud", "konto": "", "username": "her@icloud.com", "password": "abcdefghijklmnop",
                           "area": ["mail", "calendar", "contacts"]})
        page = await c.get("/setup?anbieter=webde&schritt=eintragen", headers={"Cf-Access-Jwt-Assertion": token()})
        assert "kommt bisher von iCloud" in page.text
        r = await _connect(c, {"anbieter": "webde", "konto": "", "username": "her@web.de", "password": "Web-App-Pass1",
                               "area": ["mail"]}, "anbieter=webde&schritt=eintragen")
    assert r.status_code == 200
    env = accounts.env_overlay(str(tmp_path))
    assert env["IMAP_HOST"] == "imap.web.de" and env["IMAP_SECURITY"] == "ssl" and env["SMTP_HOST"] == "smtp.web.de"
    assert env["SMTP_PORT"] == "587" and env["SMTP_SECURITY"] == "starttls" and env["MAIL_PASSWORD"] == "Web-App-Pass1"
    assert env["CALDAV_USERNAME"] == "her@icloud.com" and env["CALDAV_PASSWORD"] == "abcd-efgh-ijkl-mnop"
    assert env["ICLOUD_EMAIL_ADDRESS"] == "her@web.de" and env["ENABLE_MAIL"] == "true"
    saved = accounts.load(str(tmp_path))
    assert [a["areas"] for a in saved["accounts"]] == [["mail"], ["calendar", "contacts"]]


async def test_setup_refused_sign_in_saves_nothing(setup_env, tmp_path):
    from icloud_mcp import accounts
    app, _checked, timers = setup_env
    async with client_for(app) as c:
        r = await _connect(c, {"anbieter": "webde", "konto": "", "username": "her@web.de", "password": "wrong", "area": ["mail"]},
                           "anbieter=webde&schritt=eintragen")
    assert r.status_code == 400 and "POP3- und IMAP-Zugriff" in r.text and "nichts gespeichert" in r.text
    assert accounts.load(str(tmp_path)) is None and timers == []


async def test_setup_edit_keeps_the_password_and_chooses_calendars(setup_env, tmp_path):
    from icloud_mcp import accounts
    app, checked, _timers = setup_env
    hdr = {"Cf-Access-Jwt-Assertion": token()}
    async with client_for(app) as c:
        await _connect(c, {"anbieter": "icloud", "konto": "", "username": "her@icloud.com", "password": "abcdefghijklmnop",
                           "area": ["mail", "calendar"]})
        acct_id = accounts.load(str(tmp_path))["accounts"][0]["id"]
        r = await _connect(c, {"anbieter": "icloud", "konto": acct_id, "username": "her@icloud.com", "password": "",
                               "area": ["calendar"], "read_only": "1"}, f"konto={acct_id}&schritt=eintragen")
        assert r.status_code == 200 and checked[-1]["password"] == "abcd-efgh-ijkl-mnop"
        home = await c.get("/setup", headers=hdr)
        assert "Deine verbundenen Konten" in home.text and "(2 von 2)" in home.text and "nur lesen" in home.text
        r = await c.post("/setup", data={**_form_tokens(home.text.split('value="calendars"', 1)[0].rsplit("<form", 1)[1]),
                                         "action": "calendars", "cal": ["https://caldav.example.com/1/work/"]}, headers=hdr)
    assert r.status_code == 200 and "Gespeichert." in r.text
    saved = accounts.load(str(tmp_path))
    assert saved["calendars_visible"] == ["https://caldav.example.com/1/work/"] and saved["accounts"][0]["read_only"]
    env = accounts.env_overlay(str(tmp_path))
    assert env["ENABLE_MAIL"] == "false" and env["READ_ONLY_AREAS"] == "calendar"


async def test_setup_disconnect_asks_first(setup_env, tmp_path):
    from icloud_mcp import accounts
    app, _checked, _timers = setup_env
    hdr = {"Cf-Access-Jwt-Assertion": token()}
    async with client_for(app) as c:
        await _connect(c, {"anbieter": "gmail", "konto": "", "username": "her@gmail.com", "password": "abcd efgh ijkl mnop",
                           "area": ["mail", "calendar"]}, "anbieter=gmail&schritt=eintragen")
        saved = accounts.load(str(tmp_path))
        assert saved["accounts"][0]["areas"] == ["mail"] and saved["accounts"][0]["password"] == "abcdefghijklmnop"
        acct_id = saved["accounts"][0]["id"]
        ask = await c.get(f"/setup?trennen={acct_id}", headers=hdr)
        assert "trennen?" in ask.text and "myaccount.google.com/apppasswords" in ask.text
        r = await c.post("/setup", data={**_form_tokens(ask.text), "action": "remove", "konto": acct_id}, headers=hdr)
    assert r.status_code == 200 and accounts.load(str(tmp_path))["accounts"] == []
    assert accounts.env_overlay(str(tmp_path))["ICLOUD_USERNAME"] == accounts.PENDING_USERNAME


async def test_setup_other_provider_needs_valid_servers(setup_env):
    app, _checked, _timers = setup_env
    async with client_for(app) as c:
        r = await c.get("/setup?anbieter=other&schritt=eintragen&imap=imap.example.org&smtp=", headers={"Cf-Access-Jwt-Assertion": token()})
        ok = await c.get("/setup?anbieter=other&schritt=eintragen&imap=imap.example.org&smtp=smtp.example.org%3A465&caldav=&carddav=",
                         headers={"Cf-Access-Jwt-Assertion": token()})
    assert r.status_code == 400 and "beide Server" in r.text
    assert ok.status_code == 200 and 'value="smtp.example.org:465"' in ok.text and 'value="calendar"' not in ok.text


async def test_setup_rejects_a_forged_form(setup_env, tmp_path):
    from icloud_mcp import accounts
    app, checked, _timers = setup_env
    async with client_for(app) as c:
        r = await c.post("/setup", data={"action": "connect", "anbieter": "icloud", "username": "x@icloud.com", "password": "abcdefghijklmnop",
                                         "area": "mail", "exp": "9999999999", "tok": "0" * 64}, headers={"Cf-Access-Jwt-Assertion": token()})
    assert r.status_code == 403 and checked == [] and accounts.load(str(tmp_path)) is None


async def test_overview_shows_the_deployment_account(sso_app, setup_env):
    _app, checked, _timers = setup_env
    async with client_for(sso_app) as c:
        r = await c.get("/setup", headers={"Cf-Access-Jwt-Assertion": token()})
    assert "Deine verbundenen Konten" in r.text and "me@icloud.com" in r.text and checked[0]["password"] == "aaaa-bbbb-cccc-dddd"
    assert "aaaa-bbbb" not in r.text


async def test_account_overrides_win_over_the_environment(tmp_path, monkeypatch):
    from icloud_mcp.admin import save_override
    for k, v in dict(ICLOUD_USERNAME="pending@setup.invalid", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", MCP_PUBLIC_URL=BASE,
                     MCP_OWNER_PASSWORD=PASSWORD, DATA_DIR=str(tmp_path)).items():
        monkeypatch.setenv(k, v)
    save_override(str(tmp_path), "icloud_username", "her@icloud.com")
    save_override(str(tmp_path), "icloud_display_name", "Her Name")
    s = Settings.from_env()
    assert s.username == "her@icloud.com" and s.imap_username == "her@icloud.com" and s.caldav_username == "her@icloud.com"
    assert s.email_address == "her@icloud.com" and s.display_name == "Her Name"


async def test_store_account_proves_the_new_account(tmp_path, monkeypatch):
    import dataclasses as dc
    from icloud_mcp import admin, mail
    from icloud_mcp.config import read_overrides
    for k, v in dict(ICLOUD_USERNAME="pending@setup.invalid", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", MCP_PUBLIC_URL=BASE,
                     MCP_OWNER_PASSWORD=PASSWORD, DATA_DIR=str(tmp_path)).items():
        monkeypatch.setenv(k, v)
    tried = []
    class Conn:
        def logout(self): pass
    monkeypatch.setattr(mail.MailService, "_login", lambda self: (tried.append((self.s.imap_username if hasattr(self, "s") else None)), Conn())[1])
    s = Settings.from_env()
    assert await admin.check_and_store_app_password(s, "abcdefghijklmnop", "test", account={"username": "not-an-address"}) is not None
    err = await admin.check_and_store_app_password(s, "abcd efgh ijkl mnop", "test",
                                                   account={"username": "her@icloud.com", "email_address": "", "display_name": "Her"})
    assert err is None
    o = read_overrides(str(tmp_path))
    assert o["icloud_username"] == "her@icloud.com" and o["icloud_app_password"] == "abcd-efgh-ijkl-mnop"
    assert o["icloud_display_name"] == "Her" and "icloud_email_address" not in o
