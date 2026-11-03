"""Owner sign-in through Tailscale (tailscale serve sets Tailscale-User-Login): believed only from a trusted proxy address."""
import httpx
import pytest

from icloud_mcp import access_sso
from icloud_mcp.config import Settings
from test_dashboard import BASE, web  # noqa: F401  (web is a fixture)

OWNER = "owner@example.org"


class Req:
    def __init__(self, login: str | None, host: str | None):
        self.headers = {access_sso.TAILSCALE_HEADER: login} if login is not None else {}
        self.client = type("C", (), {"host": host})() if host is not None else None


@pytest.mark.parametrize("host, login, expected", [
    ("127.0.0.1", OWNER, OWNER),                 # the proxy on the same host
    ("172.20.0.5", OWNER.upper(), OWNER),        # inside the proxy's network range, any case
    ("::ffff:127.0.0.1", OWNER, OWNER),          # IPv4 seen through an IPv6 socket
    ("100.64.1.2", OWNER, None),                 # someone else in the tailnet sending the header themselves
    ("127.0.0.1", "stranger@example.org", None), # the proxy, but a login that is not the owner's
    ("127.0.0.1", "", None),
    (None, OWNER, None),
])
def test_the_header_counts_only_from_a_trusted_proxy_and_for_an_owner_login(host, login, expected):
    ts = access_sso.TailscaleOwner((OWNER,), ("127.0.0.1", "172.20.0.0/16"))
    assert ts.owner(Req(login, host)) == expected


def test_logins_without_a_trusted_proxy_leave_it_off(monkeypatch):
    monkeypatch.setenv("ICLOUD_USERNAME", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "aaaa-bbbb-cccc-dddd")
    monkeypatch.setenv("OWNER_TAILSCALE_LOGINS", OWNER)
    assert access_sso.from_settings(Settings.from_env()) is None
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "127.0.0.1")
    assert access_sso.from_settings(Settings.from_env()).tailscale is not None


def client_from(app, host):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 40000)), base_url=BASE, follow_redirects=False)


async def test_the_dashboard_signs_the_owner_in_through_the_trusted_proxy_only(web):
    app, mcp, *_ = web(OWNER_TAILSCALE_LOGINS=OWNER, TRUSTED_PROXY_IPS="127.0.0.1")
    headers = {"tailscale-user-login": OWNER}
    async with client_from(app, "100.64.9.9") as c:          # straight from the tailnet, header made up by the client
        r = await c.get("/dashboard/signin", headers=headers)
        assert r.status_code == 200 and "Sign in" in r.text
        assert (await c.get("/dashboard")).status_code == 303
    async with client_from(app, "127.0.0.1") as c:
        r = await c.get("/dashboard/signin", headers={"tailscale-user-login": "stranger@example.org"})
        assert r.status_code == 200 and "Sign in" in r.text
        r = await c.get("/dashboard/signin", headers=headers)
        assert r.status_code == 303 and r.headers["location"] == "/dashboard"
        assert (await c.get("/dashboard")).status_code == 200
    entries = mcp._icloud_dashboard.audit.entries(limit=5)
    assert any(e.get("via") == f"Tailscale ({OWNER})" for e in entries)


async def test_outbox_and_connect_page_take_the_tailscale_owner(web):
    app, *_ = web(OWNER_TAILSCALE_LOGINS=OWNER, TRUSTED_PROXY_IPS="127.0.0.1", DASHBOARD="false")
    async with client_from(app, "127.0.0.1") as c:
        r = await c.get("/outbox", headers={"tailscale-user-login": OWNER})
        assert r.status_code == 200 and f"Signed in as {OWNER}" in r.text
        r = await c.get("/outbox", headers={"tailscale-user-login": "stranger@example.org"})
        assert f"Signed in as" not in r.text
    async with client_from(app, "10.0.0.7") as c:
        r = await c.get("/outbox", headers={"tailscale-user-login": OWNER})
        assert "Signed in as" not in r.text


def test_passkeys_from_before_carry_their_address_and_a_new_address_does_not_offer_them(tmp_path):
    import json
    from icloud_mcp.passkeys import PasskeyStore
    old = {"user_id": "AAAAAAAAAAAAAAAAAAAAAA", "keys": [{"id": "a1", "public_key": "pk", "sign_count": 0, "name": "iPhone",
                                                          "created_at": 1.0}]}
    (tmp_path / "passkeys.json").write_text(json.dumps(old))
    before = PasskeyStore(str(tmp_path), "https://old.example.org")
    assert [k.name for k in before.list()] == ["iPhone"]
    assert json.loads((tmp_path / "passkeys.json").read_text())["keys"][0]["rp_id"] == "old.example.org"
    after = PasskeyStore(str(tmp_path), "https://new.example.ts.net")
    assert after.list() == []                                    # not offered, so the browser is never asked for it
    assert [k["name"] for k in json.loads((tmp_path / "passkeys.json").read_text())["keys"]] == ["iPhone"]   # still on disk
    assert [k.name for k in PasskeyStore(str(tmp_path), "https://old.example.org").list()] == ["iPhone"]      # moving back works
