"""Notices of decisions (docs/SENDGATE.md, package 9b) on the dashboard: the owner's host lists, the service the dashboard runs, and
the health line. Every host, receiver and request here is made up. Each test says what it protects, the wrong result it rejects and
what a green run does not prove."""
from icloud_mcp.config import Settings
from icloud_mcp.gate import notices as N
from icloud_mcp.settings import SPECS

from test_dashboard import client, signed_in, web  # noqa: F401  (web is a fixture)
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
