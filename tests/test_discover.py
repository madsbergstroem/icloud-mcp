"""Finding another provider's servers from an address (discover.py, POST /dashboard/api/accounts/discover): Thunderbird's
autoconfig order, the ISP database with the domain only, the MX host, RFC 6186 SRV records, and the checks that keep it to
encrypted servers on public hosts. No request leaves the test: HTTP and DNS are faked, and every domain is made up."""
from types import SimpleNamespace

import httpx
import pytest

from icloud_mcp import discover as D
from test_dashboard import client, signed_in, web  # noqa: F401  (web is a fixture)
from test_services import OWNER, SSO, _hdr  # noqa: F401

CONFIG = """<?xml version="1.0"?>
<clientConfig version="1.1"><emailProvider id="example.net"><domain>example.net</domain>
  <incomingServer type="imap"><hostname>imap.%EMAILDOMAIN%</hostname><port>143</port><socketType>plain</socketType>
    <username>%EMAILADDRESS%</username><authentication>password-cleartext</authentication></incomingServer>
  <incomingServer type="imap"><hostname>imap.%EMAILDOMAIN%</hostname><port>993</port><socketType>SSL</socketType>
    <username>%EMAILLOCALPART%</username><authentication>password-cleartext</authentication></incomingServer>
  <outgoingServer type="smtp"><hostname>smtp.example.net</hostname><port>587</port><socketType>SSL</socketType>
    <username>%EMAILADDRESS%</username><authentication>password-cleartext</authentication></outgoingServer>
  <outgoingServer type="smtp"><hostname>smtp.example.net</hostname><port>587</port><socketType>STARTTLS</socketType>
    <username>%EMAILADDRESS%</username><authentication>password-cleartext</authentication></outgoingServer>
</emailProvider></clientConfig>"""

OAUTH_ONLY = CONFIG.replace("password-cleartext", "OAuth2")


@pytest.fixture(autouse=True)
def public_hosts(monkeypatch):
    """Every made-up host resolves to a public address (a well-known public resolver's), except names with "intranet" in them."""
    monkeypatch.setattr(D, "_addresses", lambda host: ["127.0.0.1"] if "intranet" in host else ["1.1.1.1"])


def http(pages: dict[str, str], seen: list[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        for prefix, body in pages.items():
            if str(request.url).startswith(prefix):
                return httpx.Response(200, text=body)
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def dns(records: dict[tuple[str, str], list]):
    async def resolve(name, rtype):
        return records.get((name, rtype), [])
    return resolve


def mx(host):
    return SimpleNamespace(exchange=host + ".", preference=10)


def srv(target, port, priority=0, weight=0):
    return SimpleNamespace(target=target + ".", port=port, priority=priority, weight=weight)


def test_the_file_gives_the_first_encrypted_server_that_fits_how_this_server_connects():
    got = D.parse_autoconfig(CONFIG, "alex@example.net")
    # plain IMAP is skipped; SSL on 587 would be opened with STARTTLS here, so the STARTTLS entry is taken
    assert got == {"imap": "imap.example.net:993", "smtp": "smtp.example.net:587", "username": "alex", "notes": []}


def test_a_provider_with_oauth_only_is_named_and_gives_no_servers():
    assert D.parse_autoconfig(OAUTH_ONLY, "alex@example.net") == {
        "notes": ["This provider signs mail apps in through its own login page, which is not supported here."]}
    assert D.parse_autoconfig("<html>not a config</html>", "alex@example.net") is None


async def test_the_providers_own_file_comes_first_and_gets_the_address():
    seen = []
    async with http({"https://autoconfig.example.net/mail/config-v1.1.xml": CONFIG}, seen) as c:
        got = await D.discover("Alex@Example.net", client=c, resolve=dns({}), dav=lambda a, k: "")
    assert got["found"] and got["source"] == "provider" and got["servers"]["imap"] == "imap.example.net:993"
    assert seen == ["https://autoconfig.example.net/mail/config-v1.1.xml?emailaddress=Alex%40example.net"]


async def test_the_isp_database_and_the_mx_host_get_the_domain_only():
    """The provider's own file here allows OAuth2 only; the MX host's file allows a password, and the note about OAuth2 goes."""
    seen = []
    async with http({"https://autoconfig.example.com/mail/config-v1.1.xml": CONFIG,
                     "https://autoconfig.example.net/mail/config-v1.1.xml": OAUTH_ONLY}, seen) as c:
        got = await D.discover("alex@example.net", client=c, resolve=dns({("example.net", "MX"): [mx("mx01.mailhost.example.com")]}),
                               dav=lambda a, k: "")
    assert got["source"] == "mx" and got["servers"]["smtp"] == "smtp.example.net:587" and got["notes"] == []
    assert seen == ["https://autoconfig.example.net/mail/config-v1.1.xml?emailaddress=alex%40example.net",
                    "https://example.net/.well-known/autoconfig/mail/config-v1.1.xml?emailaddress=alex%40example.net",
                    "https://autoconfig.thunderbird.net/v1.1/example.net",
                    "https://autoconfig.example.com/mail/config-v1.1.xml"]            # base domain of the MX host, no address
    assert all("alex" not in u for u in seen[2:])


async def test_srv_records_are_the_last_resort_and_stay_inside_the_domain():
    records = {("_imaps._tcp.example.net", "SRV"): [srv("imap.elsewhere.example.org", 993), srv("imap.example.net", 993, priority=5)],
               ("_submissions._tcp.example.net", "SRV"): [srv("smtp.example.net", 587)],          # 587 would be STARTTLS here
               ("_submission._tcp.example.net", "SRV"): [srv("smtp.example.net", 587)]}
    async with http({}, []) as c:
        got = await D.discover("alex@example.net", client=c, resolve=dns(records), dav=lambda a, k: "")
    assert got["source"] == "dns" and got["servers"]["imap"] == "imap.example.net:993" and got["servers"]["smtp"] == "smtp.example.net:587"


async def test_a_host_on_a_private_network_is_never_asked():
    seen = []
    async with http({"https://autoconfig.intranet.example.net/": CONFIG}, seen) as c:
        got = await D.discover("alex@intranet.example.net", client=c, resolve=dns({}), dav=lambda a, k: "")
    assert not got["found"] and "autoconfig.intranet" not in " ".join(seen)


async def test_calendar_and_contacts_come_from_rfc_6764_and_something_that_is_no_address_is_refused():
    async with http({}, []) as c:
        got = await D.discover("alex@example.net", client=c, resolve=dns({}),
                               dav=lambda a, k: f"https://dav.example.net/{k}/" if k == "caldav" else "")
        assert got["found"] and got["servers"] == {"imap": "", "smtp": "", "caldav": "https://dav.example.net/caldav/", "carddav": ""}
        with pytest.raises(ValueError):
            await D.discover("not an address", client=c, resolve=dns({}), dav=lambda a, k: "")
        with pytest.raises(ValueError):
            await D.discover("alex@10.0.0.1", client=c, resolve=dns({}), dav=lambda a, k: "")  # privacy-ok: a made-up IP literal


async def test_the_route_needs_the_owner_and_saves_nothing(web, tmp_path, monkeypatch):
    async def fake(address):
        if "@" not in address:
            raise ValueError("Enter a full email address first, like alex@example.org.")
        return {"found": True, "source": "ispdb", "servers": {"imap": "imap.example.net:993", "smtp": "smtp.example.net:587",
                                                               "caldav": "", "carddav": ""}, "username": address, "notes": []}
    monkeypatch.setattr(D, "discover", fake)
    app, *_ = web(**SSO)
    async with client(app) as c:
        hdr = await _hdr(c)
        bare = {k: v for k, v in hdr.items() if k not in OWNER}
        assert (await c.post("/dashboard/api/accounts/discover", headers=bare, json={"email": "alex@example.net"})).status_code == 403
        bad = await c.post("/dashboard/api/accounts/discover", headers=hdr, json={"email": "alex"})
        assert bad.status_code == 400 and bad.json()["error"].startswith("Enter a full email address")
        r = (await c.post("/dashboard/api/accounts/discover", headers=hdr, json={"email": "alex@example.net"})).json()
    assert r["servers"]["imap"] == "imap.example.net:993" and r["source"] == "ispdb"
    assert not (tmp_path / "accounts.json").exists()
