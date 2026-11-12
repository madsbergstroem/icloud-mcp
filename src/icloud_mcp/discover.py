"""Find the servers of another email and calendar provider from an email address ("Find servers" on Connect a service).

Mail follows Thunderbird's autoconfig order (github.com/thunderbird/autoconfig/wiki, "The autoconfig file" and "The ISP
database"), then RFC 6186:

1. https://autoconfig.<domain>/mail/config-v1.1.xml?emailaddress=<address>   the provider's own file
2. https://<domain>/.well-known/autoconfig/mail/config-v1.1.xml?emailaddress=<address>
3. https://autoconfig.thunderbird.net/v1.1/<domain>                            Thunderbird's ISP database, domain only
4. the base domain of the domain's MX record: its autoconfig subdomain and the ISP database, both without the address
5. DNS SRV records _imaps._tcp / _imap._tcp and _submissions._tcp / _submission._tcp (RFC 6186, RFC 8314)

Calendar and contacts use RFC 6764 through caldav.discovery (SRV, TXT and /.well-known/caldav or carddav), which the caldav
library already brings; it accepts TLS only and only hosts inside the queried domain.

Only encrypted servers are taken (SSL or STARTTLS), and only where the port matches how this server connects (993 and 465 as
SSL, any other port as STARTTLS: accounts.security). A provider that signs in only with OAuth2 is reported in `notes`. An SRV
or MX target outside the queried domain is not followed for SRV (RFC 6186 section 6). Every host is checked to be a public
name that resolves to public addresses before anything is fetched. Nothing is saved: the owner reviews what was found and the
usual sign-in check runs on Connect. The log has the outcome only, never the address or the domain.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import xml.etree.ElementTree as ET
from typing import Any, Awaitable, Callable

import httpx

from . import accounts as A

log = logging.getLogger(__name__)

ISPDB = "https://autoconfig.thunderbird.net/v1.1/"
TIMEOUT = 6.0                 # seconds per request
MAX_BYTES = 200_000           # an autoconfig file is a few KB
_DOMAIN = re.compile(r"(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_EMAIL = re.compile(r"([^\s@]+)@([^\s@]+)")

Resolve = Callable[[str, str], Awaitable[list[Any]]]


def _addresses(host: str) -> list[str]:
    try:
        return [i[4][0] for i in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]
    except OSError:
        return []


def _public(host: str) -> bool:
    """A real public host name that resolves only to public addresses (no IP literal, no localhost, no private network)."""
    found = _addresses(host) if _DOMAIN.fullmatch(host) else []
    return bool(found) and all(ipaddress.ip_address(a).is_global for a in found)


def _in_domain(host: str, domain: str) -> bool:
    host, domain = host.rstrip(".").lower(), domain.rstrip(".").lower()
    return host == domain or host.endswith("." + domain)


def _base(host: str) -> str:
    """The "base" domain of an MX host (mx01.emailprovider.tld -> emailprovider.tld), as Thunderbird takes it: the last two labels."""
    return ".".join(host.rstrip(".").lower().split(".")[-2:])


def _fill(pattern: str, address: str) -> str:
    local, _, domain = address.partition("@")
    return pattern.replace("%EMAILADDRESS%", address).replace("%EMAILLOCALPART%", local).replace("%EMAILDOMAIN%", domain)


def parse_autoconfig(text: str, address: str) -> dict[str, Any] | None:
    """The first usable IMAP and SMTP server of a clientConfig file: {"imap", "smtp", "username", "notes"}, or None."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    provider = root.find("emailProvider")
    if root.tag != "clientConfig" or provider is None:
        return None
    out: dict[str, Any] = {"notes": []}
    oauth_only = False
    for kind, tag in (("imap", "incomingServer"), ("smtp", "outgoingServer")):
        for srv in provider.findall(tag):
            if srv.get("type") != kind:
                continue
            host = _fill((srv.findtext("hostname") or "").strip(), address).lower()
            try:
                port = int((srv.findtext("port") or "").strip())
            except ValueError:
                continue
            sock = (srv.findtext("socketType") or "").strip().upper()
            auth = {(a.text or "").strip() for a in srv.findall("authentication")}
            if auth and not auth & {"password-cleartext", "password-encrypted"}:
                oauth_only = oauth_only or "OAuth2" in auth
                continue
            # only encrypted, and only how this server connects: 993 and 465 directly over TLS, other ports with STARTTLS
            if sock not in ("SSL", "STARTTLS") or ("ssl" if sock == "SSL" else "starttls") != A.security(port):
                continue
            if not A.host_port(f"{host}:{port}", port)[0]:
                continue
            out[kind] = f"{host}:{port}"
            if kind == "imap" and (user := (srv.findtext("username") or "").strip()):
                out["username"] = _fill(user, address)
            break
    if oauth_only and not ("imap" in out and "smtp" in out):
        out["notes"].append("This provider signs mail apps in through its own login page, which is not supported here.")
    return out if "imap" in out and "smtp" in out else ({"notes": out["notes"]} if out["notes"] else None)


async def _fetch(client: httpx.AsyncClient, url: str, host: str) -> str | None:
    if not await asyncio.to_thread(_public, host):
        return None
    try:
        async with client.stream("GET", url) as r:
            if r.status_code != 200:
                return None
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    return None
            return body.decode("utf-8", "replace")
    except httpx.HTTPError:
        return None


async def _dns(name: str, rtype: str) -> list[Any]:
    import dns.asyncresolver
    import dns.exception
    try:
        return list(await dns.asyncresolver.resolve(name, rtype, lifetime=TIMEOUT))
    except (dns.exception.DNSException, OSError):
        return []


async def find_mail(address: str, client: httpx.AsyncClient, resolve: Resolve = _dns) -> dict[str, Any]:
    """{"found": bool, "source", "imap", "smtp", "username", "notes"} for the mail servers of an address."""
    domain = address.rsplit("@", 1)[1].lower()
    q = httpx.QueryParams({"emailaddress": address})
    notes: list[str] = []
    tries = [("provider", f"https://autoconfig.{domain}/mail/config-v1.1.xml?{q}", f"autoconfig.{domain}"),
             ("provider", f"https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml?{q}", domain),
             ("ispdb", ISPDB + domain, "autoconfig.thunderbird.net")]
    mx = sorted(await resolve(domain, "MX"), key=lambda r: getattr(r, "preference", 0))
    if mx and (base := _base(str(mx[0].exchange))) and base != domain and _DOMAIN.fullmatch(base):
        tries += [("mx", f"https://autoconfig.{base}/mail/config-v1.1.xml", f"autoconfig.{base}"), ("mx", ISPDB + base, "autoconfig.thunderbird.net")]
    for source, url, host in tries:
        text = await _fetch(client, url, host)
        got = parse_autoconfig(text, address) if text else None
        if got:
            notes += got.pop("notes", [])
            if "imap" in got:
                return {"found": True, "source": source, **got, "notes": []}     # an earlier file's notes no longer matter
    # RFC 6186: SRV records, encrypted first; a target "." means the service is not offered, one outside the domain is skipped
    found: dict[str, str] = {}
    for kind, labels in (("imap", ("_imaps._tcp", "_imap._tcp")), ("smtp", ("_submissions._tcp", "_submission._tcp"))):
        for label in labels:
            recs = sorted(await resolve(f"{label}.{domain}", "SRV"), key=lambda r: (r.priority, -r.weight))
            for r in recs:
                target = str(r.target).rstrip(".").lower()
                if not target or not _in_domain(target, domain):
                    continue
                implicit = label in ("_imaps._tcp", "_submissions._tcp")
                if (A.security(r.port) == "ssl") != implicit:
                    continue                                    # this server would connect to that port the other way
                found[kind] = f"{target}:{r.port}"
                break
            if kind in found:
                break
    if "imap" in found and "smtp" in found:
        return {"found": True, "source": "dns", **found, "username": address, "notes": []}
    return {"found": False, "notes": notes}


def _dav(address: str, kind: str) -> str:
    """The CalDAV or CardDAV address of RFC 6764 discovery, or ""."""
    from caldav import discovery
    domain = address.rsplit("@", 1)[1]
    if any(not ipaddress.ip_address(a).is_global for a in _addresses(domain)):
        return ""                                       # its /.well-known address would be a private host
    try:
        info = discovery.discover_service(address, kind, timeout=int(TIMEOUT))
    except Exception:  # noqa: BLE001 - discovery that trips reports nothing found
        return ""
    if info is None or not info.url.startswith("https://") or not _public(info.hostname):
        return ""
    return info.url


async def discover(address: str, *, client: httpx.AsyncClient | None = None, resolve: Resolve = _dns,
                   dav: Callable[[str, str], str] = _dav) -> dict[str, Any]:
    """Everything that can be found for an address: {"found", "source", "servers": {imap, smtp, caldav, carddav}, "username",
    "notes"}. Raises ValueError for something that is not an email address with a public domain."""
    m = _EMAIL.fullmatch(address.strip())
    if not m or not _DOMAIN.fullmatch(m.group(2).lower()):
        raise ValueError("Enter a full email address first, like alex@example.org.")
    address = f"{m.group(1)}@{m.group(2).lower()}"
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, headers={"user-agent": "icloud-mcp autoconfig"})
    try:
        mail, caldav, carddav = await asyncio.gather(find_mail(address, client, resolve), asyncio.to_thread(dav, address, "caldav"),
                                                     asyncio.to_thread(dav, address, "carddav"))
    finally:
        if own:
            await client.aclose()
    servers = {"imap": mail.get("imap", ""), "smtp": mail.get("smtp", ""), "caldav": caldav, "carddav": carddav}
    found = any(servers.values())
    log.info("Server discovery: mail %s, calendar %s, contacts %s", mail.get("source", "not found"),
             "found" if caldav else "not found", "found" if carddav else "not found")
    return {"found": found, "source": mail.get("source", "dns" if found else ""), "servers": servers,
            "username": mail.get("username", address), "notes": mail.get("notes", [])}
