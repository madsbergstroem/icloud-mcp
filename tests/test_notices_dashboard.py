"""Notices of decisions (docs/SENDGATE.md, package 9b) on the dashboard: the owner's host lists, the service the dashboard runs, and
the health line. Every host, receiver and request here is made up. Each test says what it protects, the wrong result it rejects and
what a green run does not prove."""
from icloud_mcp.config import Settings
from icloud_mcp.gate import notices as N
from icloud_mcp.settings import SPECS

from test_dashboard import PASSWORD, client, queue, signed_in, web  # noqa: F401  (web is a fixture)
from test_settings import api, stale

HOSTS = dict(NOTICE_HOSTS="hooks.example.org, Other.Example.Net", NOTICE_PRIVATE_HOSTS="other.example.net")


def test_the_host_lists_come_from_the_environment_and_empty_means_none(tmp_path, monkeypatch):
    """protects=NOTICE_HOSTS and NOTICE_PRIVATE_HOSTS as settings (lower-cased lists); empty means no notice leaves; fails_when=a
    list is not read, keeps its case, or empty is taken as everyone; no_claim=none beyond protects."""
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path), **HOSTS).items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    assert s.notice_hosts == ("hooks.example.org", "other.example.net") and s.notice_private_hosts == ("other.example.net",)
    monkeypatch.delenv("NOTICE_HOSTS")
    assert Settings.from_env().notice_hosts == ()
    assert SPECS["NOTICE_HOSTS"].loosens("", "hooks.example.org") and SPECS["NOTICE_PRIVATE_HOSTS"].loosens("a.example.org", "a.example.org,b.example.org")
    assert not SPECS["NOTICE_HOSTS"].loosens("a.example.org,b.example.org", "a.example.org")


async def test_adding_a_host_without_a_recent_sign_in_asks_to_confirm(web):  # noqa: F811
    """protects=criterion 13, first half: a host added to NOTICE_HOSTS or NOTICE_PRIVATE_HOSTS needs a recent sign-in;
    fails_when=it is saved without one, or removing a host asks; no_claim=receivers (tests below, after the API exists)."""
    app, mcp, _, _ = web()
    d = mcp._icloud_dashboard
    async with client(app) as c:
        token = await signed_in(c)
        stale(d)
        for key in ("NOTICE_HOSTS", "NOTICE_PRIVATE_HOSTS"):
            r = await c.post("/dashboard/api/settings/notices", headers=api(token), json={"values": {key: "hooks.example.org"}})
            assert r.status_code == 403 and r.json()["confirm"] is True, key
        assert not (d.sessions.path.parent / "settings.json").exists()
    app, mcp, _, _ = web(NOTICE_HOSTS="hooks.example.org,other.example.net")
    d = mcp._icloud_dashboard
    async with client(app) as c:
        token = await signed_in(c)
        stale(d)
        r = await c.post("/dashboard/api/settings/notices", headers=api(token), json={"values": {"NOTICE_HOSTS": "hooks.example.org"}})
        assert r.status_code == 200 and r.json()["restart_needed"] is True


def test_the_dashboard_runs_one_notice_service_with_the_owners_hosts_and_no_thread_without_receivers(web):  # noqa: F811
    """protects=the wiring: the dashboard's service reads the store's ledger with NOTICE_HOSTS, NOTICE_PRIVATE_HOSTS and
    AUDIT_KEEP_DAYS, names a mail's kind as approval_status does, and starts no thread while nobody receives; fails_when=it is
    missing, has other lists, or a thread runs for nobody; no_claim=delivery (tests/test_notices.py)."""
    app, mcp, _, _ = web(AUDIT_KEEP_DAYS="30", **HOSTS)
    svc = mcp._icloud_dashboard.notices
    assert isinstance(svc, N.NoticeService) and svc.hosts == {"hooks.example.org", "other.example.net"}
    assert svc.private_hosts == {"other.example.net"} and svc.keep_days == 30 and svc.autostart is True
    assert svc.kinds("mail.send") == "mail" and svc._thread is None
    assert svc.receivers() == []


def test_a_receiver_failing_for_a_day_shows_on_the_health_check(web, monkeypatch):  # noqa: F811
    """protects=the owner hears of a receiver that keeps failing, through icloud_check_health; fails_when=no line, or a line with
    the secret; no_claim=the deployment's egress list (outside the server)."""
    app, mcp, _, _ = web(**HOSTS)
    svc = mcp._icloud_dashboard.notices
    svc.autostart = False
    monkeypatch.setattr(N.NoticeService, "problems", lambda self, now=None: ["Notices to hooks.example.org have failed for more than "
                                                                             "24 hours (connect_failed)."])
    _, secret = svc.add("https://hooks.example.org/sendgate")
    report = mcp._icloud_health()
    assert report["notices"] == {"ok": False, "problem": "Notices to hooks.example.org have failed for more than 24 hours (connect_failed)."}
    assert secret[6:] not in str(report)


# -- receivers through the dashboard API ------------------------------------------------------------------------------------------
URL = "https://hooks.example.org/sendgate"


async def confirm(c, token):
    """Confirm it is you: the password again in the same session, which hands out a fresh token."""
    r = await c.post("/dashboard/api/signin", json={"password": PASSWORD}, headers=api(token))
    assert r.status_code == 200 and r.json()["confirmed"]
    return (await c.get("/dashboard/api/session")).json()["csrf"]


def quiet(mcp):
    """The dashboard's notice service with a fake receiver and resolver, and no thread: the test calls run_once itself."""
    from test_notices import PUBLIC_IP, Receiver
    svc = mcp._icloud_dashboard.notices
    svc.autostart = False
    svc.transport, svc.resolve = Receiver(200), (lambda h, p: [PUBLIC_IP])
    return svc


async def test_adding_or_widening_a_receiver_needs_a_recent_sign_in_and_the_secret_is_shown_once(web, caplog):  # noqa: F811
    """protects=criteria 12 and 13: adding a receiver, giving it a new URL, widening its filter or making a new secret asks for a
    recent sign-in; the secret is in the answer that makes it and in no other answer, log line, activity entry, health answer or
    settings answer; fails_when=any of these is done without a fresh sign-in, or a secret shows up anywhere else;
    no_claim=what the owner does with the secret after copying it."""
    import logging
    caplog.set_level(logging.DEBUG)
    app, mcp, _, _ = web(**HOSTS)
    d = mcp._icloud_dashboard
    svc = quiet(mcp)
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post("/dashboard/api/notices/add", headers=api(token), json={"url": "http://hooks.example.org/x"})
        assert r.status_code == 400 and "https" in r.json()["error"]
        r = await c.post("/dashboard/api/notices/add", headers=api(token), json={"url": "https://elsewhere.example.net/x"})
        assert r.status_code == 400 and "Allowed hosts" in r.json()["error"]
        stale(d)
        r = await c.post("/dashboard/api/notices/add", headers=api(token), json={"url": URL})
        assert r.status_code == 403 and r.json()["confirm"] is True and svc.receivers() == []
        token = await confirm(c, token)
        r = await c.post("/dashboard/api/notices/add", headers=api(token), json={"url": URL, "filter": {"statuses": ["sent"]}})
        assert r.status_code == 200, r.text
        first, rid = r.json()["secret"], r.json()["receiver"]["id"]
        assert first.startswith("whsec_") and r.json()["receiver"]["host"] == "hooks.example.org"
        stale(d)
        for path, body in ((f"/notices/{rid}/change", {"url": "https://other.example.net/hook"}),
                           (f"/notices/{rid}/change", {"filter": {"statuses": []}}), (f"/notices/{rid}/rotate", {})):
            r = await c.post("/dashboard/api" + path, headers=api(token), json=body)
            assert r.status_code == 403 and r.json()["confirm"] is True, path
        r = await c.post(f"/dashboard/api/notices/{rid}/change", headers=api(token), json={"filter": {"statuses": ["sent"], "kinds": ["mail"]}})
        assert r.status_code == 200 and "secret" not in r.json()                                    # narrower: no step-up
        token = await confirm(c, token)
        second = (await c.post(f"/dashboard/api/notices/{rid}/rotate", headers=api(token))).json()["secret"]
        third = (await c.post(f"/dashboard/api/notices/{rid}/change", headers=api(token), json={"url": "https://other.example.net/hook"})).json()["secret"]
        assert len({first, second, third}) == 3
        seen = [(await c.get(p)).text for p in ("/dashboard/api/notices", "/dashboard/api/settings", "/dashboard/api/activity?days=1",
                                                "/dashboard/activity.json", "/dashboard/api/server")]
    seen += [str(mcp._icloud_health()), caplog.text, (d.sessions.path.parent / "audit.jsonl").read_text()]
    for secret in (first, second, third):
        assert all(secret[6:] not in text for text in seen)
    mine = [e for e in reversed(d.audit.entries()) if e["action"].startswith("notice.")]
    assert [e["action"] for e in mine] == ["notice.receiver_added", "notice.receiver_changed", "notice.receiver_rotated", "notice.receiver_changed"]
    assert all(e["details"] == {"host": e["details"]["host"]} for e in mine) and "other.example.net" in mine[-1]["summary"]


async def test_the_owner_sees_each_receiver_removes_one_and_sends_dead_notices_again(web, monkeypatch):  # noqa: F811
    """protects=what the owner sees per receiver (last success, waiting, not delivered, refused, last error) and the two actions on
    it: send dead notices again, remove (which drops what waits); fails_when=a count is wrong, a resend does not go out, or a
    removed receiver keeps getting notices; no_claim=the page's look (no mock yet, see the report of package 9b)."""
    from test_notices import Receiver
    app, mcp, _, _ = web(**HOSTS)
    svc = quiet(mcp)
    async with client(app) as c:
        token = await signed_in(c)
        rid = (await c.post("/dashboard/api/notices/add", headers=api(token), json={"url": URL})).json()["receiver"]["id"]
        svc.transport = Receiver(500)
        qid = queue(mcp)
        monkeypatch.setattr(N, "BACKOFF", (0,))                                   # one attempt, then dead
        svc.run_once()
        got = (await c.get("/dashboard/api/notices")).json()
        st = got["receivers"][0]["status"]
        assert got["hosts"] == ["hooks.example.org", "other.example.net"] and got["max"] == 5
        assert (st["dead"], st["pending"], st["last_error"], st["last_ok_at"]) == (1, 0, "http_500", None)
        svc.transport = ok = Receiver(200)
        r = await c.post(f"/dashboard/api/notices/{rid}/resend", headers=api(token))
        assert r.json()["count"] == 1
        svc.run_once()
        assert [b["data"]["id"] for b in ok.bodies()] == [qid]
        st = (await c.get("/dashboard/api/notices")).json()["receivers"][0]["status"]
        assert (st["dead"], st["delivered"]) == (0, 1) and st["last_ok_at"]
        assert (await c.post("/dashboard/api/notices/nope/remove", headers=api(token))).status_code == 404
        r = await c.post(f"/dashboard/api/notices/{rid}/remove", headers=api(token))
        assert r.status_code == 200 and (await c.get("/dashboard/api/notices")).json()["receivers"] == []
        queue(mcp, subject="Another one")
        svc.run_once()
        assert len(ok.calls) == 1


async def test_a_real_mail_request_end_to_end_leaves_no_content_in_its_notices(web):  # noqa: F811
    """protects=criterion 6 end to end: a mail with a known subject, text and recipient, queued by the tool and approved on the
    dashboard, makes notices with none of them, and with the kind approval_status names; fails_when=any of them is in a body or
    header; no_claim=every other action type (tests/test_notices.py covers four made-up ones at the store)."""
    app, mcp, _, sent = web(**HOSTS)
    svc = quiet(mcp)
    async with client(app) as c:
        token = await signed_in(c)
        await c.post("/dashboard/api/notices/add", headers=api(token), json={"url": URL})
        qid = queue(mcp, subject="Dinner with Jana at the lake", body="Tom brings the salad")
        it = (await c.get(f"/dashboard/api/items/external:{qid}")).json()
        r = await c.post(f"/dashboard/api/items/external:{qid}/approve", headers=api(token), json={"sha": it["sha"]})
        assert r.status_code == 200 and sent
    svc.run_once()
    calls = svc.transport.calls
    assert [(b["data"]["status"], b["data"]["kind"], b["data"]["action_type"]) for b in svc.transport.bodies()] == [
        ("waiting", "mail", "mail.send"), ("sent", "mail", "mail.send")]
    text = repr([(c_["headers"], c_["body"]) for c_ in calls])
    for value in ("Dinner with Jana", "Tom brings", "bob@example.org", "me@icloud.com"):
        assert value not in text


async def test_notice_routes_need_a_session_this_origin_and_the_token(web):  # noqa: F811
    """protects=the owner-only door: no session, another origin or no CSRF token changes nothing; fails_when=any of them adds a
    receiver; no_claim=none beyond protects."""
    app, mcp, _, _ = web(**HOSTS)
    async with client(app) as c:
        assert (await c.get("/dashboard/api/notices")).status_code == 401
        assert (await c.post("/dashboard/api/notices/add", json={"url": URL})).status_code == 401
        token = await signed_in(c)
        assert (await c.post("/dashboard/api/notices/add", headers={"origin": "https://mcp.example.com"}, json={"url": URL})).status_code == 403
        assert (await c.post("/dashboard/api/notices/add", headers={**api(token), "origin": "https://evil.example.net"}, json={"url": URL})).status_code == 403
    assert mcp._icloud_dashboard.notices.receivers() == []


def test_no_tool_reads_or_sets_a_receiver(web):  # noqa: F811
    """protects=receivers come from the owner only (the decisions table: "no tool can touch them"); fails_when=a tool's name,
    description or schema speaks of receivers, webhooks or notice hosts; no_claim=a future MCP events transport (out of scope)."""
    _, mcp, _, _ = web(**HOSTS)
    tools = mcp._tool_manager.list_tools()
    text = " ".join(f"{t.name} {t.description} {t.parameters}" for t in tools).lower()
    assert tools and not any(w in text for w in ("receiver", "webhook", "notice_host", "notices.json", "whsec_"))
