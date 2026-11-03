"""Settings on the dashboard (GET/POST /dashboard/api/settings): stored in DATA_DIR/settings.json, only what DASHBOARD_EDITABLE allows, checked before saving,
and a change that weakens protection asks for the owner again."""
import json


from icloud_mcp.config import DASHBOARD_EDITABLE, Settings

from test_dashboard import PASSWORD, SAME, client, signed_in, web  # noqa: F401  (the web fixture)


def api(token):
    return {**SAME, "x-csrf": token}


def stale(d):
    for rec in d.sessions._items.values():
        rec["auth_at"] -= 600


def field(st, slug, key):
    g = next(g for g in st["groups"] if g["slug"] == slug)
    return next(f for f in g["fields"] if f["key"] == key)


def test_dashboard_values_win_over_the_environment_but_only_for_editable_keys(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MAX_RECIPIENTS="25", OAUTH_ALLOWED_REDIRECT_HOSTS="claude.ai").items():
        monkeypatch.setenv(k, v)
    (tmp_path / "settings.json").write_text(json.dumps({"MAX_RECIPIENTS": "5", "OAUTH_ALLOWED_REDIRECT_HOSTS": "evil.example.net",
                                                        "MCP_PORT": "1"}))
    s = Settings.from_env()
    assert s.max_recipients == 5 and s.settings_active == ("MAX_RECIPIENTS",)
    assert s.allowed_redirect_hosts == ("claude.ai",) and s.port == 8000
    assert "OAUTH_ALLOWED_REDIRECT_HOSTS" not in DASHBOARD_EDITABLE and "MCP_PUBLIC_URL" not in DASHBOARD_EDITABLE
    monkeypatch.setenv("DASHBOARD_SETTINGS", "off")
    assert Settings.from_env().max_recipients == 25


async def test_a_change_is_saved_converted_and_logged(web):  # noqa: F811
    app, mcp, _, _ = web()
    async with client(app) as c:
        token = await signed_in(c)
        st = (await c.get("/dashboard/api/settings")).json()
        f = field(st, "mail", "MAX_ATTACHMENT_BYTES")
        assert st["editable"] is True and f["value"] == "5" and f["source"] == "default" and f["set_here"] is False
        assert st["restart_needed"] is False
        r = await c.post("/dashboard/api/settings/mail", headers=api(token), json={"values": {"MAX_ATTACHMENT_BYTES": "20"}})
        assert r.status_code == 200 and r.json()["restart_needed"] is True
        stored = json.loads((mcp._icloud_dashboard.sessions.path.parent / "settings.json").read_text())
        assert stored == {"MAX_ATTACHMENT_BYTES": str(20 * 1024 * 1024)}                 # only what changed, in bytes
        st = (await c.get("/dashboard/api/settings")).json()
        f = field(st, "mail", "MAX_ATTACHMENT_BYTES")
        assert st["restart_needed"] is True and f["value"] == "20" and f["source"] == "dashboard" and f["set_here"] is True
        r = await c.post("/dashboard/api/settings/mail", headers=api(token), json={"values": {"MAX_ATTACHMENT_BYTES": 20}})
        assert r.json()["note"] == "Nothing changed."
    entry = mcp._icloud_dashboard.audit.entries()[0]
    assert entry["action"] == "settings.changed" and "Largest attachment 5 MiB → 20 MiB" in entry["summary"]


async def test_writes_need_the_csrf_token_and_this_origin(web):  # noqa: F811
    app, mcp, _, _ = web()
    async with client(app) as c:
        assert (await c.get("/dashboard/api/settings")).status_code == 401
        token = await signed_in(c)
        body = {"values": {"MAX_RECIPIENTS": "3"}}
        assert (await c.post("/dashboard/api/settings/mail", headers=SAME, json=body)).status_code == 403
        assert (await c.post("/dashboard/api/settings/mail", headers={**api(token), "origin": "https://evil.example.net"}, json=body)).status_code == 403
    assert not (mcp._icloud_dashboard.sessions.path.parent / "settings.json").exists()


async def test_weakening_protection_asks_again(web):  # noqa: F811
    app, mcp, _, _ = web()
    d = mcp._icloud_dashboard
    async with client(app) as c:
        token = await signed_in(c)
        stale(d)
        body = {"values": {"SEND_REQUIRES_APPROVAL": False}}
        r = await c.post("/dashboard/api/settings/mail", headers=api(token), json=body)
        assert r.status_code == 403 and r.json()["confirm"] is True
        assert not (d.sessions.path.parent / "settings.json").exists()
        r = await c.post("/dashboard/api/signin", headers=api(token), json={"password": PASSWORD})
        assert r.status_code == 200 and r.json()["confirmed"] is True
        r = await c.post("/dashboard/api/settings/mail", headers=api(token), json=body)       # the app repeats the call
        assert r.status_code == 200
    assert json.loads((d.sessions.path.parent / "settings.json").read_text()) == {"SEND_REQUIRES_APPROVAL": "false"}
    assert d.audit.entries()[0]["details"]["weakened"] == ["SEND_REQUIRES_APPROVAL"]


async def test_a_stricter_change_does_not_ask_again(web):  # noqa: F811
    app, mcp, _, _ = web()
    d = mcp._icloud_dashboard
    async with client(app) as c:
        token = await signed_in(c)
        stale(d)
        r = await c.post("/dashboard/api/settings/mail", headers=api(token), json={"values": {"MAX_ATTACHMENT_BYTES": "2"}})
        assert r.status_code == 200
    assert json.loads((d.sessions.path.parent / "settings.json").read_text()) == {"MAX_ATTACHMENT_BYTES": str(2 * 1024 * 1024)}


async def test_values_the_server_could_not_start_with_are_refused(web):  # noqa: F811
    app, mcp, _, _ = web()
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post("/dashboard/api/settings/calendar", headers=api(token), json={"values": {"DEFAULT_TIMEZONE": "Mars/Olympus"}})
        assert r.status_code == 400 and "not a time zone" in r.json()["fields"]["DEFAULT_TIMEZONE"]
        r = await c.post("/dashboard/api/settings/areas", headers=api(token), json={"values": {"TOOLS": "mail,no_such_tool"}})
        assert r.status_code == 400 and "no_such_tool" in r.json()["error"]
        r = await c.post("/dashboard/api/settings/mail", headers=api(token), json={"values": {"MAX_RECIPIENTS": "abc"}})
        assert r.status_code == 400 and "enter a number" in r.json()["fields"]["MAX_RECIPIENTS"]
        r = await c.post("/dashboard/api/settings/mail", headers=api(token),
                         json={"values": {"MAX_RECIPIENTS": "abc", "MAX_ATTACHMENT_BYTES": "9999"}})
        assert r.status_code == 400 and set(r.json()["fields"]) == {"MAX_RECIPIENTS", "MAX_ATTACHMENT_BYTES"}   # every field named
    assert not (mcp._icloud_dashboard.sessions.path.parent / "settings.json").exists()


async def test_reset_goes_back_to_the_environment(web):  # noqa: F811
    app, mcp, _, _ = web()
    path = mcp._icloud_dashboard.sessions.path.parent / "settings.json"
    path.write_text(json.dumps({"DEFAULT_TIMEZONE": "Asia/Tokyo"}))
    async with client(app) as c:
        token = await signed_in(c)
        f = field((await c.get("/dashboard/api/settings")).json(), "calendar", "DEFAULT_TIMEZONE")
        assert f["value"] == "Asia/Tokyo" and f["source"] == "dashboard" and f["set_here"] is True
        r = await c.post("/dashboard/api/settings/calendar/reset", headers=api(token), json={"key": "DEFAULT_TIMEZONE"})
        assert r.status_code == 200 and "follows the server again (Europe/Berlin)" in r.json()["note"]
        r = await c.post("/dashboard/api/settings/calendar/reset", headers=api(token), json={"key": "DEFAULT_TIMEZONE"})
        assert r.status_code == 404                                                    # nothing set here any more
    assert json.loads(path.read_text()) == {}


async def test_view_only_shows_but_does_not_change(web):  # noqa: F811
    app, mcp, _, _ = web(DASHBOARD_SETTINGS="view")
    path = mcp._icloud_dashboard.sessions.path.parent / "settings.json"
    async with client(app) as c:
        token = await signed_in(c)
        st = (await c.get("/dashboard/api/settings")).json()
        assert st["editable"] is False and field(st, "mail", "MAX_RECIPIENTS")["value"]
        server = {label: value for sec in st["server"] for label, value in sec["rows"]}
        assert "https://mcp.example.com" in server.values()
        r = await c.post("/dashboard/api/settings/mail", headers=api(token), json={"values": {"MAX_RECIPIENTS": "3"}})
        assert r.status_code == 403
        r = await c.post("/dashboard/api/settings/mail/reset", headers=api(token), json={"key": "MAX_RECIPIENTS"})
        assert r.status_code == 404
        r = await c.post("/dashboard/api/settings/accounts/app-password", headers=api(token), json={"password": "abcd-efgh-ijkl-mnop"})
        assert r.status_code == 403
    assert not path.exists()


async def test_settings_can_be_left_out(web):  # noqa: F811
    app, *_ = web(DASHBOARD_SETTINGS="off")
    async with client(app) as c:
        token = await signed_in(c)
        assert (await c.get("/dashboard/api/session")).json()["settings"] is False
        assert (await c.get("/dashboard/api/settings")).status_code == 404
        r = await c.post("/dashboard/api/settings/mail", headers=api(token), json={"values": {"MAX_RECIPIENTS": "3"}})
        assert r.status_code == 404


async def test_the_sending_choice_sets_both_switches(web):  # noqa: F811
    app, mcp, _, _ = web()
    path = mcp._icloud_dashboard.sessions.path.parent / "settings.json"
    async with client(app) as c:
        token = await signed_in(c)
        st = (await c.get("/dashboard/api/settings")).json()
        mail = next(g for g in st["groups"] if g["slug"] == "mail")
        assert mail["send_mode"] == "approval" and "ALLOW_SEND" not in {f["key"] for f in mail["fields"]}   # one choice, not two switches
        r = await c.post("/dashboard/api/settings/mail", headers=api(token), json={"values": {"ALLOW_SEND": False, "SEND_REQUIRES_APPROVAL": True}})
        assert r.status_code == 200                                                   # drafts
        assert json.loads(path.read_text()) == {"ALLOW_SEND": "false"}
        st = (await c.get("/dashboard/api/settings")).json()
        assert next(g for g in st["groups"] if g["slug"] == "mail")["send_mode"] == "drafts"
        r = await c.post("/dashboard/api/settings/mail", headers=api(token), json={"values": {"ALLOW_SEND": True, "SEND_REQUIRES_APPROVAL": False}})
        assert r.status_code == 200                                                   # direct (the sign-in is recent)
        assert json.loads(path.read_text()) == {"ALLOW_SEND": "true", "SEND_REQUIRES_APPROVAL": "false"}
        st = (await c.get("/dashboard/api/settings")).json()
        assert next(g for g in st["groups"] if g["slug"] == "mail")["send_mode"] == "direct"


async def test_a_reset_that_weakens_protection_needs_a_fresh_sign_in(web):  # noqa: F811
    app, mcp, _, _ = web(SEND_REQUIRES_APPROVAL="false")
    d = mcp._icloud_dashboard
    path = d.sessions.path.parent / "settings.json"
    path.write_text(json.dumps({"SEND_REQUIRES_APPROVAL": "true"}))
    async with client(app) as c:
        token = await signed_in(c)
        stale(d)
        body = {"key": "SEND_REQUIRES_APPROVAL"}
        r = await c.post("/dashboard/api/settings/mail/reset", headers=api(token), json=body)
        assert r.status_code == 403 and r.json()["confirm"] is True
        assert json.loads(path.read_text()) == {"SEND_REQUIRES_APPROVAL": "true"}
        await c.post("/dashboard/api/signin", headers=api(token), json={"password": PASSWORD})
        r = await c.post("/dashboard/api/settings/mail/reset", headers=api(token), json=body)
        assert r.status_code == 200 and "now follows the server again" in r.json()["note"]
    assert json.loads(path.read_text()) == {}


async def test_restart_now_restarts_the_server(web):  # noqa: F811
    import threading
    app, mcp, _, _ = web()
    d = mcp._icloud_dashboard
    done = threading.Event()
    d.exit_fn = done.set
    async with client(app) as c:
        token = await signed_in(c)
        assert (await c.post("/dashboard/api/settings/restart", headers=SAME)).status_code == 403 and not done.wait(1)
        r = await c.post("/dashboard/api/settings/restart", headers=api(token))
        assert r.status_code == 200 and "Restarting" in r.json()["note"]
    assert done.wait(3)
    assert d.audit.entries()[0]["action"] == "server.restart"


def test_every_weakening_change_is_recognised():
    from icloud_mcp.settings import SPECS
    weakens = {
        "IMESSAGE_HIDDEN_CHATS": [("Family, Bank", "Family"), ("Family", "")],
        "IMESSAGE_NEVER_SEND": [("+4930000000", "")],
        "MAIL_MAX_AGE_DAYS": [("30", "0"), ("30", "90")],
        "IMESSAGE_MAX_AGE_DAYS": [("7", "0"), ("7", "8")],
        "OWNER_ADDRESSES": [("me@example.org", "me@example.org, other@example.net"), ("", "other@example.net")],
        "MAX_BODY_CHARS": [("30000", "60000")],
        "ICLOUD_EMAIL_ADDRESS": [("me@example.org", "you@example.org")],
        "MAX_ATTACHMENT_BYTES": [("5", "20")],
    }
    keeps = {
        "IMESSAGE_HIDDEN_CHATS": [("Family", "Family, Bank")],
        "MAIL_MAX_AGE_DAYS": [("0", "30"), ("30", "7")],
        "OWNER_ADDRESSES": [("me@example.org, other@example.net", "me@example.org")],
        "MAX_BODY_CHARS": [("30000", "10000")],
    }
    for key, pairs in weakens.items():
        for old, new in pairs:
            assert SPECS[key].loosens(old, new), (key, old, new)
    for key, pairs in keeps.items():
        for old, new in pairs:
            assert not SPECS[key].loosens(old, new), (key, old, new)


def test_two_saves_at_once_keep_both_changes(tmp_path):
    from icloud_mcp.settings import SettingsStore
    store = SettingsStore(str(tmp_path))
    first, second = store.read(), store.read()                   # both pages were opened before either was saved
    store.update({"MAX_RECIPIENTS": "5"})
    store.update({"OUTBOX_MAX": "3"})
    assert store.read() == {"MAX_RECIPIENTS": "5", "OUTBOX_MAX": "3"} and first == second == {}
    store.update(remove=("MAX_RECIPIENTS",))
    assert store.read() == {"OUTBOX_MAX": "3"}
