"""The owner's dashboard (the Ionic app at /dashboard and its JSON API): sessions, CSRF and origin checks, passkey sign-in,
approvals, activity and security steps. The old page addresses lead into the app."""
import base64
import contextlib
import json
import re
import time

import httpx
import pytest

from icloud_mcp.config import Settings
from icloud_mcp.mail import MailService
from icloud_mcp.server import build_app, create_server

from softauthn import SoftAuthenticator
from test_outbox import FakeIMAP

BASE = "https://mcp.example.com"
PASSWORD = "correct-horse-battery"
SAME = {"origin": BASE}


@pytest.fixture
def web(tmp_path, monkeypatch):
    def make(**extra):
        env = dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", MCP_PUBLIC_URL=BASE,
                   MCP_OWNER_PASSWORD=PASSWORD, DATA_DIR=str(tmp_path), ALLOW_SEND="true", WARMUP_ON_START="false",
                   DEFAULT_TIMEZONE="Europe/Berlin", **extra)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        s = Settings.from_env()
        fake, sent = FakeIMAP(), []

        @contextlib.contextmanager
        def fake_imap(self):
            yield fake

        monkeypatch.setattr(MailService, "imap", fake_imap)
        monkeypatch.setattr(MailService, "resolve_folder", lambda self, c, name: {"sent": "Sent Messages", "drafts": "Drafts"}.get(name.lower(), name))
        monkeypatch.setattr(MailService, "_smtp_send", lambda self, msg, rcpts: sent.append((msg, list(rcpts))) or {})
        mcp, provider = create_server(s)
        return build_app(s, mcp), mcp, provider, sent
    return make


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, follow_redirects=False)


def api(token: str) -> dict[str, str]:
    """Headers of a write from the app: this origin and the session's CSRF token."""
    return {**SAME, "x-csrf": token}


async def signed_in(c):
    """Signs in with the owner password through the API and returns the session's CSRF token."""
    r = await c.post("/dashboard/api/signin", json={"password": PASSWORD}, headers=SAME)
    assert r.status_code == 200, r.text
    s = (await c.get("/dashboard/api/session")).json()
    assert s["signed_in"]
    return s["csrf"]


def queue(mcp, subject="Lunch <b>now</b>", body="See you & bye"):
    mail = mcp._icloud_dashboard.sources["mail"].mail
    return mail.send(to=["bob@example.org"], subject=subject, body=body)["outbox_id"]


def sha_of(mcp, kind="mail"):
    return mcp._icloud_dashboard.sources[kind].pending()[0].sha256


async def test_everything_needs_a_session(web):
    app, mcp, _, sent = web()
    qid = queue(mcp)
    async with client(app) as c:
        r = await c.get("/dashboard")                                   # the app shell holds no data: it loads for anyone
        assert r.status_code == 200 and "<ion-app>" in r.text
        assert "script-src 'self'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "same-origin"
        s = (await c.get("/dashboard/api/session")).json()
        assert s["signed_in"] is False and s["password_login"] is True and s["passkeys"] is False and "csrf" not in s
        for path in ("/dashboard/api/today", "/dashboard/api/state", f"/dashboard/api/items/mail:{qid}", "/dashboard/api/activity",
                     "/dashboard/api/security", "/dashboard/api/server", "/dashboard/api/push", "/dashboard/activity.json",
                     "/dashboard/state.json"):
            assert (await c.get(path)).status_code == 401, path
        r = await c.post(f"/dashboard/api/items/mail:{qid}/approve", json={"sha": sha_of(mcp)}, headers=SAME)
        assert r.status_code == 401 and sent == []


async def test_the_old_page_addresses_lead_into_the_app(web):
    app, *_ = web()
    async with client(app) as c:
        for path, to in (("/dashboard/approvals", "/dashboard"), ("/dashboard/approvals?item=mail:abc", "/dashboard#/item/mail:abc"),
                         ("/dashboard/approvals?item=x%22%3E", "/dashboard"), ("/dashboard/attach/abc", "/dashboard#/item/mail:abc"),
                         ("/dashboard/activity", "/dashboard#/activity"), ("/dashboard/security", "/dashboard#/settings"),
                         ("/dashboard/settings", "/dashboard#/settings"), ("/dashboard/settings/mail", "/dashboard#/settings"),
                         ("/dashboard/signin", "/dashboard"), ("/dashboard/app", "/dashboard")):
            r = await c.get(path)
            assert r.status_code == 303 and r.headers["location"] == to, path


async def test_password_sign_in_sets_a_strict_cookie_and_wrong_passwords_lock_out(web):
    app, mcp, provider, _ = web()
    async with client(app) as c:
        r = await c.post("/dashboard/api/signin", json={"password": "wrong-password-1"}, headers=SAME)
        assert r.status_code == 401
        r = await c.post("/dashboard/api/signin", json={"password": PASSWORD}, headers=SAME)
        assert r.status_code == 200
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("__Host-icloud-dashboard=") and "HttpOnly" in cookie and "Secure" in cookie
        assert "samesite=strict" in cookie.lower() and "Path=/" in cookie
        assert (await c.get("/dashboard/api/today")).status_code == 200
    stored = json.loads((mcp._icloud_dashboard.sessions.path).read_text())
    token = cookie.split(";")[0].split("=", 1)[1]
    assert token not in json.dumps(stored)                      # only the hash is on disk
    for _ in range(9):
        provider.record_failure()
    async with client(app) as c:
        r = await c.post("/dashboard/api/signin", json={"password": PASSWORD}, headers=SAME)
        assert r.status_code == 429
    log = mcp._icloud_dashboard.audit.entries()
    assert [e["ok"] for e in log if e["action"] == "signin.password"] == [True, False]


async def test_other_origins_and_missing_csrf_are_refused(web):
    app, mcp, _, sent = web()
    qid = queue(mcp)
    async with client(app) as c:
        r = await c.post("/dashboard/api/signin", json={"password": PASSWORD}, headers={"origin": "https://evil.example.net"})
        assert r.status_code == 403
        token = await signed_in(c)
        url, body = f"/dashboard/api/items/mail:{qid}/approve", {"sha": sha_of(mcp)}
        assert (await c.post(url, json=body, headers=SAME)).status_code == 403                                   # no csrf
        assert (await c.post(url, json=body, headers={**api("wrong"), "origin": BASE})).status_code == 403       # wrong csrf
        assert (await c.post(url, json=body, headers={**api(token), "origin": "https://evil.example.net"})).status_code == 403
        assert (await c.post(url, json=body, headers={"x-csrf": token})).status_code == 403                      # no origin at all
        assert sent == []


async def test_approve_sends_once_and_is_logged(web):
    app, mcp, _, sent = web()
    qid = queue(mcp)
    async with client(app) as c:
        token = await signed_in(c)
        it = (await c.get(f"/dashboard/api/items/mail:{qid}")).json()
        assert it["title"] == "Lunch <b>now</b>" and it["text"].strip() == "See you & bye"     # plain text; the app escapes it
        r = await c.post(f"/dashboard/api/items/mail:{qid}/approve", json={"sha": it["sha"]}, headers=api(token))
        assert r.status_code == 200 and r.json() == {"ok": True, "note": "Sent to bob@example.org."} and len(sent) == 1
        r = await c.post(f"/dashboard/api/items/mail:{qid}/approve", json={"sha": it["sha"]}, headers=api(token))  # a replay
        assert r.status_code == 409 and r.json()["ok"] is False and len(sent) == 1
        a = (await c.get("/dashboard/api/activity")).json()
        first = a["days"][0]["entries"][0]
        assert first["who"] == "You" and first["what"] == "approved email “Lunch <b>now</b>”"
        assert a["chain"]["ok"] is True and a["chain"]["entries"] >= 2                        # none changed since they were written
        sent_today = (await c.get("/dashboard/api/today")).json()["sent_today"]
        assert [e["what"] for e in sent_today] == ["approved email “Lunch <b>now</b>”"]


async def test_a_changed_message_is_not_sent_and_discard_sends_nothing(web):
    app, mcp, _, sent = web()
    qid = queue(mcp)
    async with client(app) as c:
        token = await signed_in(c)
        url = f"/dashboard/api/items/mail:{qid}"
        assert (await c.post(f"{url}/approve", json={"sha": "0" * 64}, headers=api(token))).status_code == 409
        r = await c.post(f"{url}/discard", json={"sha": sha_of(mcp)}, headers=api(token))
        assert r.status_code == 200 and r.json() == {"ok": True, "note": "Discarded. Nothing was sent."}
        assert sent == [] and mcp._icloud_dashboard.sources["mail"].pending() == []
        assert (await c.get(url)).status_code == 404
    assert [e["action"] for e in mcp._icloud_dashboard.audit.entries()][0] == "approval.discarded"


async def test_sessions_survive_a_restart_and_sign_out_ends_them(web):
    app, *_ = web()
    async with client(app) as c:
        token = await signed_in(c)
        cookies = dict(c.cookies)
    app2, *_ = web()                                            # a new process reading the same data folder
    async with client(app2) as c:
        c.cookies.update(cookies)
        assert (await c.get("/dashboard/api/session")).json()["signed_in"] is True
        assert (await c.post("/dashboard/api/signout", headers=SAME)).status_code == 403      # needs the CSRF token
        r = await c.post("/dashboard/api/signout", headers=api(token))
        assert r.status_code == 200
        c.cookies.update(cookies)                               # the old cookie no longer opens anything
        assert (await c.get("/dashboard/api/session")).json()["signed_in"] is False
        assert (await c.get("/dashboard/api/today")).status_code == 401


async def test_sensitive_steps_ask_again_after_five_minutes(web):
    app, mcp, _, _ = web()
    async with client(app) as c:
        token = await signed_in(c)
        d = mcp._icloud_dashboard
        for rec in d.sessions._items.values():
            rec["auth_at"] -= 600
        r = await c.post("/dashboard/api/security/password", json={"password": "a-new-owner-password"}, headers=api(token))
        assert r.status_code == 403 and r.json()["confirm"] is True
        assert not (d.sessions.path.parent / "overrides.json").exists()
        r = await c.post("/dashboard/api/signin", json={"password": PASSWORD}, headers=api(token))
        assert r.status_code == 200 and r.json()["confirmed"] is True
        r = await c.post("/dashboard/api/security/password", json={"password": "short"}, headers=api(token))
        assert r.status_code == 400 and "at least 12" in r.json()["error"]
        r = await c.post("/dashboard/api/security/password", json={"password": "a-new-owner-password"}, headers=api(token))
        assert r.status_code == 200 and "applies after the server restarts" in r.json()["note"]
    assert json.loads((mcp._icloud_dashboard.sessions.path.parent / "overrides.json").read_text())["owner_password"] == "a-new-owner-password"


async def test_passkey_registration_and_sign_in(web):
    pytest.importorskip("webauthn")
    app, mcp, _, _ = web()
    dev = SoftAuthenticator(BASE, "mcp.example.com")
    async with client(app) as c:
        token = await signed_in(c)
        assert (await c.post("/dashboard/passkey/options", json={"purpose": "register"}, headers=SAME)).status_code == 401   # no CSRF token
        r = await c.post("/dashboard/passkey/options", json={"purpose": "register"}, headers=api(token))
        opts = r.json()
        r = await c.post("/dashboard/passkey/verify", headers=api(token),
                         json={"purpose": "register", "ticket": opts["ticket"], "credential": dev.create(opts["publicKey"]), "name": "Desk key"})
        assert r.status_code == 200, r.text
        assert [k["name"] for k in (await c.get("/dashboard/api/security")).json()["passkeys"]] == ["Desk key"]
    async with client(app) as c:                                # a fresh browser
        s = (await c.get("/dashboard/api/session")).json()
        assert s["signed_in"] is False and s["passkeys"] is True
        assert (await c.post("/dashboard/passkey/options", json={"purpose": "signin"},
                             headers={"origin": "https://evil.example.net"})).status_code == 403
        opts = (await c.post("/dashboard/passkey/options", json={"purpose": "signin"}, headers=SAME)).json()
        r = await c.post("/dashboard/passkey/verify", headers=SAME,
                         json={"purpose": "signin", "ticket": opts["ticket"], "credential": dev.get(opts["publicKey"])})
        assert r.status_code == 200 and r.json()["next"] == "/dashboard" and "__Host-icloud-dashboard" in r.headers["set-cookie"]
        assert (await c.get("/dashboard/api/today")).status_code == 200
        assert (await c.get("/dashboard/api/session")).json()["via"] == "passkey “Desk key”"
    via = [e.get("via") for e in mcp._icloud_dashboard.audit.entries() if e["action"] == "signin.passkey"]
    assert via == ["passkey “Desk key”"]


async def test_registering_a_passkey_needs_a_recent_sign_in(web):
    pytest.importorskip("webauthn")
    app, mcp, _, _ = web()
    async with client(app) as c:
        token = await signed_in(c)
        for rec in mcp._icloud_dashboard.sessions._items.values():
            rec["auth_at"] -= 600
        r = await c.post("/dashboard/passkey/options", json={"purpose": "register"}, headers=api(token))
        assert r.status_code == 403 and r.json() == {"error": "confirm", "confirm": True}


async def test_a_password_lockout_does_not_lock_out_the_passkey(web):
    pytest.importorskip("webauthn")
    app, mcp, provider, _ = web()
    store = mcp._icloud_dashboard.passkeys
    dev = SoftAuthenticator(BASE, "mcp.example.com")
    opts = store.registration_options()
    store.register(opts["ticket"], dev.create(opts["publicKey"]), "Phone")
    for _ in range(10):
        provider.record_failure()
    async with client(app) as c:
        r = await c.post("/dashboard/api/signin", json={"password": PASSWORD}, headers=SAME)
        assert r.status_code == 429 and "or use your passkey" in r.json()["error"]
        opts = (await c.post("/dashboard/passkey/options", json={"purpose": "signin"}, headers=SAME)).json()
        r = await c.post("/dashboard/passkey/verify", headers=SAME,
                         json={"purpose": "signin", "ticket": opts["ticket"], "credential": dev.get(opts["publicKey"])})
        assert r.status_code == 200 and (await c.get("/dashboard/api/today")).status_code == 200


async def test_password_sign_in_can_be_turned_off_once_a_passkey_exists(web):
    pytest.importorskip("webauthn")
    app, mcp, _, _ = web(OWNER_PASSWORD_LOGIN="false")
    async with client(app) as c:                                # no passkey yet: the password still works
        await signed_in(c)
    store = mcp._icloud_dashboard.passkeys
    dev = SoftAuthenticator(BASE, "mcp.example.com")
    opts = store.registration_options()
    store.register(opts["ticket"], dev.create(opts["publicKey"]), "Phone")
    async with client(app) as c:
        s = (await c.get("/dashboard/api/session")).json()
        assert s["password_login"] is False and s["passkeys"] is True
        r = await c.post("/dashboard/api/signin", json={"password": PASSWORD}, headers=SAME)
        assert r.status_code == 403 and (await c.get("/dashboard/api/today")).status_code == 401


async def test_the_script_is_served_from_here(web):
    app, *_ = web()
    async with client(app) as c:
        r = await c.get("/dashboard/passkey.js")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
        assert "navigator.credentials" in r.text


async def test_dashboard_can_be_turned_off(web):
    app, *_ = web(DASHBOARD="false")
    async with client(app) as c:
        assert (await c.get("/dashboard")).status_code == 404
        assert (await c.get("/dashboard/api/session")).status_code == 404
        assert (await c.post("/dashboard/api/signin", json={"password": PASSWORD}, headers=SAME)).status_code == 404
        assert (await c.get("/outbox")).status_code == 200          # the password page stays


async def test_a_failed_calendar_release_points_the_owner_to_the_check(web, monkeypatch):
    from icloud_mcp.cal import CalendarError, CalendarService

    def fail(self, q):
        raise CalendarError("CalDAV authentication failed. Check ICLOUD_USERNAME and the app-specific password. "
                            "Run icloud_check_health to see which service is failing.")
    monkeypatch.setattr(CalendarService, "release", fail)
    app, mcp, _, _ = web(ALLOW_CALENDAR_INVITES="approve")
    src = mcp._icloud_dashboard.sources["calendar"]
    src.cal.outbox.add(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", ["jana@example.org"],
                       {"op": "create", "calendar_url": "https://caldav.example.com/cal/", "calendar": "Club", "uid": "u1",
                        "summary": "Autumn fair", "start": "2026-10-22T18:00:00+02:00", "end": None, "location": "",
                        "description": "", "organizer": None, "href": None, "etag": None})
    async with client(app) as c:
        token = await signed_in(c)
        q = src.pending()[0]
        r = await c.post(f"/dashboard/api/items/calendar:{q.id}/approve", json={"sha": q.sha256}, headers=api(token))
        err = r.json()["error"]
        assert r.status_code == 502 and err.startswith("Not sent (still waiting)")
        assert "Check now under Settings › Server shows which service is failing." in err and "icloud_check_health" not in err
        assert len(src.pending()) == 1                                              # still waiting, can be retried
    assert mcp._icloud_dashboard.audit.entries()[0]["action"] == "approval.failed"


async def test_a_failed_release_points_the_owner_to_the_check(web, monkeypatch):
    from icloud_mcp.dashboard import MailApprovals

    def fail(self, q):
        raise RuntimeError("SMTP authentication failed. Check ICLOUD_USERNAME and the app-specific password. "
                           "Run icloud_check_health to see which service is failing.")
    monkeypatch.setattr(MailApprovals, "release", fail)
    app, mcp, _, sent = web()
    qid = queue(mcp)
    src = mcp._icloud_dashboard.sources["mail"]
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(f"/dashboard/api/items/mail:{qid}/approve", json={"sha": sha_of(mcp)}, headers=api(token))
        err = r.json()["error"]
        assert r.status_code == 502 and "Check now under Settings › Server shows which service is failing." in err and "icloud_check_health" not in err
        assert len(src.pending()) == 1 and sent == []                               # still waiting, can be retried


async def test_junk_passkey_attempts_neither_lock_the_password_nor_fill_the_log(web):
    pytest.importorskip("webauthn")
    app, mcp, provider, _ = web()
    d = mcp._icloud_dashboard
    async with client(app) as c:
        for _ in range(30):
            r = await c.post("/dashboard/passkey/verify", headers=SAME, json={"purpose": "signin", "ticket": "junk", "credential": {}})
            assert r.status_code == 401
        assert provider.login_allowed()                                              # the password still works
        assert (await c.post("/dashboard/api/signin", json={"password": PASSWORD}, headers=SAME)).status_code == 200
    assert len([e for e in d.audit.entries(limit=1000) if e["action"] == "signin.passkey" and not e["ok"]]) == 5


async def test_confirming_issues_a_new_session_token_and_needs_the_csrf_token(web):
    app, mcp, _, _ = web()
    d = mcp._icloud_dashboard
    async with client(app) as c:
        token = await signed_in(c)
        old = c.cookies.get(d.cookie)
        r = await c.post("/dashboard/api/signin", json={"password": PASSWORD}, headers=SAME)
        assert r.status_code == 403 and r.json()["error"] == "expired"                # a confirm without the CSRF token
        r = await c.post("/dashboard/api/signin", json={"password": PASSWORD}, headers=api(token))
        assert r.status_code == 200 and r.json()["confirmed"] is True and d.cookie in r.headers["set-cookie"]
        new = c.cookies.get(d.cookie)
        assert new and new != old and d.sessions.get(old) is None and d.sessions.get(new) is not None
        assert (await c.get("/dashboard/api/session")).json()["csrf"] == token        # the app keeps its CSRF token


def test_event_times_read_like_a_calendar():
    from icloud_mcp.dashboard import event_when
    assert event_when("2026-11-20T18:00:00+00:00", "2026-11-20T20:00:00+00:00", "Europe/Berlin") == "Fri 20 Nov 2026, 19:00–21:00"
    assert event_when("2026-11-20T23:00:00+01:00", "2026-11-21T01:00:00+01:00", "Europe/Berlin") == \
        "Fri 20 Nov 2026, 23:00 – Sat 21 Nov 2026, 01:00"
    assert event_when("2026-11-20", "2026-11-21", "Europe/Berlin") == "Fri 20 Nov 2026"                 # one all-day day
    assert event_when("2026-11-20", "2026-11-23", "Europe/Berlin") == "Fri 20 Nov 2026 – Sun 22 Nov 2026"
    assert event_when("2026-11-20T18:00:00", None, "Europe/Berlin") == "Fri 20 Nov 2026, 18:00"          # floating time stays as is
    assert event_when("soon", None, "Europe/Berlin") == "soon"


async def test_held_calendar_changes_show_up_and_are_released(web, monkeypatch):
    from icloud_mcp.cal import CalendarService
    released = []
    monkeypatch.setattr(CalendarService, "release", lambda self, q: released.append(q.followup["op"]) or "Added “Autumn fair”.")
    app, mcp, _, _ = web(ALLOW_CALENDAR_INVITES="approve")
    src = mcp._icloud_dashboard.sources["calendar"]
    src.cal.outbox.add(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", ["jana@example.org", "tom@example.net"],
                       {"op": "create", "calendar_url": "https://caldav.example.com/cal/", "calendar": "Club", "uid": "u1",
                        "summary": "Autumn fair", "start": "2026-10-22T18:00:00+02:00", "end": "2026-10-22T19:30:00+02:00",
                        "location": "", "description": "Bring ideas", "organizer": None, "href": None, "etag": None})
    async with client(app) as c:
        token = await signed_in(c)
        q = src.pending()[0]
        waiting = (await c.get("/dashboard/api/today")).json()["waiting"]
        assert [(w["title"], w["badge"], w["to"], w["key"]) for w in waiting] == \
            [("Autumn fair", "Invitation", "jana@example.org, tom@example.net", f"calendar:{q.id}")]
        it = (await c.get(f"/dashboard/api/items/calendar:{q.id}")).json()
        assert it["approve_label"] == "Add and invite" and "Nothing is in your calendar yet." in it["notes"]
        assert ["When", "Thu 22 Oct 2026, 18:00–19:30"] in it["rows"]              # the owner's clock, not ISO
        assert it["text"] == "Bring ideas"
        r = await c.post(f"/dashboard/api/items/calendar:{q.id}/approve", json={"sha": q.sha256}, headers=api(token))
        assert r.json() == {"ok": True, "note": "Added “Autumn fair”."} and released == ["create"] and src.pending() == []


async def test_the_app_files_are_served_to_anyone_and_the_shell_links_them(web):
    app, *_ = web()
    async with client(app) as c:
        for path, ctype in (("/dashboard/dashboard.css", "text/css"), ("/dashboard/app/app.js", "text/javascript"),
                            ("/dashboard/app/app.css", "text/css"), ("/dashboard/passkey.js", "text/javascript"),
                            ("/dashboard/install-hint.js", "text/javascript"), ("/dashboard/sw.js", "text/javascript"),
                            ("/dashboard/manifest.webmanifest", "application/manifest+json"), ("/icon-192.png", "image/png"),
                            ("/icon-512.png", "image/png")):
            r = await c.get(path)                                     # browsers fetch the manifest and the worker without the cookie
            assert r.status_code == 200 and r.headers["content-type"].startswith(ctype), path
        for path in ("/dashboard/app/index.html", "/dashboard/app/../dashboard.py", "/dashboard/vendor/../../server.py",
                     "/dashboard/app/nothing.js"):
            assert (await c.get(path)).status_code == 404, path        # only the app's own files, and the shell only at /dashboard
        worker = (await c.get("/dashboard/sw.js")).headers
        assert worker["cache-control"] == "no-cache" and worker["service-worker-allowed"] == "/dashboard"
        assert "caches." not in (await c.get("/dashboard/sw.js")).text                         # the worker stores nothing
        manifest = (await c.get("/dashboard/manifest.webmanifest")).json()
        assert manifest["start_url"] == manifest["scope"] == "/dashboard" and manifest["display"] == "standalone"
        assert {i["sizes"] for i in manifest["icons"]} == {"192x192", "512x512"}
        await signed_in(c)
        r = await c.get("/dashboard")
        csp = r.headers["content-security-policy"]
        assert "manifest-src 'self'" in csp and "worker-src 'self'" in csp and "style-src 'self'" in csp
        assert '<link rel="manifest" href="/dashboard/manifest.webmanifest' in r.text
        assert "<style>" not in r.text and "{{v}}" not in r.text
        assert re.search(r'src="/dashboard/app/app.js\?v=[0-9a-f]{10}"', r.text)
        assert re.search(r'href="/dashboard/app/app.css\?v=[0-9a-f]{10}"', r.text)
        for src in re.findall(r'(?:src|href)="(/dashboard/[^"?]+)', r.text):            # everything the shell loads is served
            assert (await c.get(src)).status_code == 200, src


async def test_the_app_icons_ride_in_the_manifest(web):
    """Browsers fetch the manifest's icons without the cookie: behind a proxy they would be its login page."""
    app, *_ = web()
    async with client(app) as c:
        manifest = await c.get("/dashboard/manifest.webmanifest")
        icons = {i["sizes"]: i["src"] for i in manifest.json()["icons"]}
        for size in ("192x192", "512x512"):
            head, _, data = icons[size].partition(",")
            assert head == "data:image/png;base64" and base64.b64decode(data).startswith(b"\x89PNG"), size
        assert manifest.headers["cache-control"] == "public, max-age=86400"
        assert (await c.get("/icon-192.png")).status_code == (await c.get("/favicon.ico")).status_code == 200
        assert (await c.get("/apple-touch-icon.png")).status_code == 200        # the routes stay for everything else
        assert "img-src 'self' data:" in (await c.get("/dashboard")).headers["content-security-policy"]


async def test_the_home_screen_icon_and_the_favicon_ride_in_the_shell_so_a_sign_in_proxy_cannot_swallow_them(web):
    """iOS asks for the icon without the session cookie, and a proxy in front answers that with its own login page."""
    app, *_ = web()
    async with client(app) as c:
        r = await c.get("/dashboard")
        embedded = re.search(r'<link rel="apple-touch-icon" href="data:image/png;base64,([A-Za-z0-9+/=]+)">', r.text)
        assert embedded and base64.b64decode(embedded.group(1)).startswith(b"\x89PNG")
        icon = re.search(r'<link rel="icon" href="data:image/x-icon;base64,([A-Za-z0-9+/=]+)">', r.text)
        assert icon and base64.b64decode(icon.group(1))[:4] == b"\x00\x00\x01\x00"     # an .ico file
        assert "/apple-touch-icon.png" not in r.text and 'href="/favicon.ico"' not in r.text   # nothing left for a proxy to intercept


async def test_the_live_state_changes_when_a_request_arrives_and_does_not_keep_the_session_alive(web):
    app, mcp, _, _ = web()
    d = mcp._icloud_dashboard
    live = {"x-live": "1"}
    async with client(app) as c:
        assert (await c.get("/dashboard/state.json")).status_code == 401
        assert (await c.get("/dashboard/api/state")).status_code == 401
        await signed_in(c)
        first = (await c.get("/dashboard/api/state", headers=live)).json()
        assert first["waiting"] == 0 and (await c.get("/dashboard/state.json", headers=live)).json()["v"] == first["v"]
        queue(mcp)
        second = (await c.get("/dashboard/api/state", headers=live)).json()
        assert second["waiting"] == 1 and second["v"] != first["v"]
        assert (await c.get("/dashboard/state.json", headers=live)).json() == {"waiting": 1, "v": second["v"]}
        assert (await c.get("/dashboard/api/today", headers=live)).json()["v"] == second["v"]
        rec = next(iter(d.sessions._items.values()))
        rec["last"] -= 120
        stale = rec["last"]
        for path in ("/dashboard/state.json", "/dashboard/api/state", "/dashboard/api/today"):
            await c.get(path, headers=live)                             # the app's own background refresh
        assert rec["last"] == stale                                     # an app left open still signs out when idle
        await c.get("/dashboard/api/today")
        assert rec["last"] > stale


async def test_a_request_with_a_warning_is_marked_and_needs_an_explicit_override(web, monkeypatch):
    from icloud_mcp.dashboard import MailApprovals
    real = MailApprovals.describe
    monkeypatch.setattr(MailApprovals, "describe", lambda self, q, ctx=None: {**real(self, q, ctx), "warnings": ["Check the address"]})
    app, mcp, _, _ = web()
    qid = queue(mcp)
    async with client(app) as c:
        await signed_in(c)
        waiting = (await c.get("/dashboard/api/today")).json()["waiting"]
        assert waiting[0]["key"] == f"mail:{qid}" and waiting[0]["warning"] is True
        assert (await c.get(f"/dashboard/api/items/mail:{qid}")).json()["warnings"] == ["Check the address"]


async def test_nothing_is_sent_without_the_override_or_when_sending_fails(web, monkeypatch):
    from icloud_mcp.dashboard import MailApprovals
    real = MailApprovals.describe
    app, mcp, _, sent = web()
    qid = queue(mcp)
    async with client(app) as c:
        token = await signed_in(c)
        src = mcp._icloud_dashboard.sources["mail"]
        url, body = f"/dashboard/api/items/mail:{qid}/approve", {"sha": sha_of(mcp)}
        monkeypatch.setattr(MailApprovals, "describe", lambda self, q, ctx=None: {**real(self, q, ctx), "needs_override": True})
        assert (await c.get(f"/dashboard/api/items/mail:{qid}")).json()["needs_override"] is True
        r = await c.post(url, json=body, headers=api(token))
        assert r.status_code == 422 and r.json()["ok"] is False and sent == [] and len(src.pending()) == 1
        monkeypatch.setattr(MailApprovals, "describe", real)

        def fail(self, q):
            raise RuntimeError("SMTP said no")
        monkeypatch.setattr(MailApprovals, "release", fail)
        r = await c.post(url, json=body, headers=api(token))
        assert r.status_code == 502 and r.json()["error"].startswith("Not sent (still waiting)") and sent == []
        assert len(src.pending()) == 1


async def test_an_override_sends_a_flagged_request(web, monkeypatch):
    from icloud_mcp.dashboard import MailApprovals
    real = MailApprovals.describe
    monkeypatch.setattr(MailApprovals, "describe", lambda self, q, ctx=None: {**real(self, q, ctx), "needs_override": True})
    app, mcp, _, sent = web()
    qid = queue(mcp)
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(f"/dashboard/api/items/mail:{qid}/approve", json={"sha": sha_of(mcp), "override": True}, headers=api(token))
        assert r.status_code == 200 and len(sent) == 1


async def test_a_request_that_has_gone_is_not_replaced_by_another(web):
    app, mcp, _, _ = web()
    gone = queue(mcp, subject="First")
    queue(mcp, subject="Second")
    mcp._icloud_dashboard.sources["mail"].discard(next(q for q in mcp._icloud_dashboard.sources["mail"].pending() if q.id == gone))
    async with client(app) as c:
        await signed_in(c)
        r = await c.get(f"/dashboard/api/items/mail:{gone}")
        assert r.status_code == 404 and r.json()["gone"] is True and "no longer waiting" in r.json()["error"]
        assert [w["title"] for w in (await c.get("/dashboard/api/today")).json()["waiting"]] == ["Second"]


async def test_one_item_never_prunes_what_is_kept_for_the_approve_check(web, monkeypatch):
    """The originals' memo is shared by every waiting item; a call about one item must not forget the others'."""
    calls = []
    real = MailService.approval_contexts
    monkeypatch.setattr(MailService, "approval_contexts", lambda self, qs, prune=False: calls.append((len(qs), prune)) or real(self, qs, prune=prune))
    app, mcp, _, _ = web()
    for i in range(7):
        queue(mcp, subject=f"Request {i}")
    async with client(app) as c:
        token = await signed_in(c)
        calls.clear()
        assert len((await c.get("/dashboard/api/today")).json()["waiting"]) == 7
        assert calls and all(n == 7 for n, _ in calls)                  # Today describes the whole queue at once
        calls.clear()
        q = mcp._icloud_dashboard.sources["mail"].pending()[0]
        await c.get(f"/dashboard/api/items/mail:{q.id}")
        await c.post(f"/dashboard/api/items/mail:{q.id}/discard", json={"sha": q.sha256}, headers=api(token))
        assert calls and not any(prune for n, prune in calls if n < 7)


async def test_the_count_of_unrecorded_failures_is_written_when_the_window_ends(web):
    app, mcp, _, _ = web()
    d = mcp._icloud_dashboard
    async with client(app) as c:
        for _ in range(12):
            await c.post("/dashboard/passkey/verify", headers=SAME, json={"purpose": "signin", "ticket": "junk", "credential": {}})
    lines = [e for e in d.audit.entries(limit=1000) if e["action"] == "signin.passkey"]
    assert len(lines) == 5                                                           # the flood has stopped here
    d.flush_failure_lines(time.time() + 601)                                         # what the timer does at the window's end
    lines = [e for e in d.audit.entries(limit=1000) if e["action"] == "signin.passkey"]
    assert len(lines) == 6 and lines[0]["summary"] == "7 more like this in 10 minutes were not recorded"
