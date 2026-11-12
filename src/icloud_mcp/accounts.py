"""The accounts a person connects in the dashboard (Settings › Connected services): which provider serves mail, calendar and
contacts, with whose sign-in.

They live in DATA_DIR/accounts.json (mode 600). Several accounts can hold the same area (mail, calendar, contacts); one of them is
the default account of that area (`defaults`), and the server runs with the defaults. The dashboard still offers one account per
area, so mail can come from another provider while calendar and contacts stay in iCloud. env_overlay() turns the file into environment
settings that Settings.from_env reads in place of the deployment's own, so the rest of the server needs no second code path.
Without the file nothing changes. Passwords stay on this server and are never logged or shown.

The file has a `version`. A file without one (the first layout) is read as version 2 in memory and rewritten only when the owner
saves something in the dashboard; nothing is copied or backed up. Older servers read a version 2 file too (they ignore what they do not
know and take the account listed last for an area).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import secrets
from typing import Any

log = logging.getLogger(__name__)

ACCOUNTS_FILE = "accounts.json"
AREAS = ("mail", "calendar", "contacts")
FILE_VERSION = 2                             # the layout of accounts.json; a file with no "version" is the first layout
AUTH_KINDS = ("app_password", "oauth")       # how an account signs in; "oauth" is reserved, nothing signs in with it yet
DEFAULT_AUTH = "app_password"
PENDING_USERNAME = "pending@setup.invalid"   # what the server runs with before anyone connected an account
CHECK_TIMEOUT = 25                           # seconds one sign-in check may take

# Two kinds of account: iCloud with Apple's own servers (the upstream project's defaults, config.py), and any other provider whose
# servers the person enters. SERVER_PRESETS only fill in those servers on the connect form ("Provider"); the account is still
# "other". Files saved before 2026-11 can name "webde" or "gmail" as the provider: they read as "other" with the preset's servers,
# so nothing they run with changes. Sources: WEB.DE hilfe.web.de/pop-imap/imap/imap-serverdaten.html, CalDAV/CardDAV hosts as
# tested by davx5.com; Gmail developers.google.com/workspace/gmail/imap/imap-smtp (calendar and contacts need Google sign-in).
PROVIDERS: dict[str, dict[str, Any]] = {
    "icloud": {"label": "iCloud", "imap": "imap.mail.me.com:993", "smtp": "smtp.mail.me.com:587",
               "caldav": "https://caldav.icloud.com", "carddav": "https://contacts.icloud.com", "areas": AREAS},
    "other": {"label": "Other email & calendar", "imap": "", "smtp": "", "caldav": "", "carddav": "", "areas": AREAS},
}
SERVER_PRESETS: dict[str, dict[str, str]] = {
    "webde": {"label": "WEB.DE", "imap": "imap.web.de:993", "smtp": "smtp.web.de:587", "caldav": "https://caldav.web.de",
              "carddav": "https://carddav.web.de"},
    "gmail": {"label": "Gmail (mail only)", "imap": "imap.gmail.com:993", "smtp": "smtp.gmail.com:465", "caldav": "", "carddav": ""},
}


def _as_known_provider(a: dict[str, Any]) -> dict[str, Any]:
    """An account under a provider this code knows: an old preset becomes "other" with that preset's servers written out."""
    p = a.get("provider")
    if p in PROVIDERS:
        return a
    ep = SERVER_PRESETS.get(p, {})
    return dict(a, provider="other", **{k: str(a.get(k) or ep.get(k, "")) for k in ("imap", "smtp", "caldav", "carddav")})


def endpoints(acct: dict[str, Any]) -> dict[str, str]:
    """IMAP, SMTP, CalDAV and CardDAV of an account: the provider's own, or what the person entered for another provider."""
    p = PROVIDERS.get(acct.get("provider", ""), PROVIDERS["other"])
    if acct.get("provider") in PROVIDERS and acct["provider"] != "other":
        return {k: p[k] for k in ("imap", "smtp", "caldav", "carddav")}
    return {k: str(acct.get(k, "")).strip() for k in ("imap", "smtp", "caldav", "carddav")}


def possible_areas(acct: dict[str, Any]) -> tuple[str, ...]:
    ep = endpoints(acct)
    have = {"mail": bool(ep["imap"] and ep["smtp"]), "calendar": bool(ep["caldav"]), "contacts": bool(ep["carddav"])}
    return tuple(a for a in PROVIDERS.get(acct.get("provider", ""), PROVIDERS["other"])["areas"] if have[a])


def host_port(value: str, default_port: int) -> tuple[str, int]:
    """'host' or 'host:port' as (host, port); ('', 0) when it is not a plain host name."""
    m = re.fullmatch(r"([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?::(\d{2,5}))?", value.strip())
    if not m:
        return "", 0
    return m.group(1).lower(), int(m.group(2) or default_port)


def security(port: int) -> str:
    return "ssl" if port in (993, 465) else "starttls"


def normalise_password(provider: str, raw: str) -> tuple[str, str | None]:
    """The password as the provider expects it, or a problem the person can fix."""
    value = raw.strip()
    if provider == "icloud":
        letters = value.replace(" ", "").replace("-", "")
        if len(letters) != 16 or not letters.isalpha():
            return "", "An app-specific password from Apple has 16 letters, like abcd-efgh-ijkl-mnop."
        return "-".join(letters[i:i + 4] for i in range(0, 16, 4)).lower(), None
    if not value or len(value) > 200:
        return "", "Enter the password."
    return value, None


# ---------------------------------------------------------------- the file
def new_id(taken: Any = ()) -> str:
    """A random, opaque account id. Nothing in it comes from the address or the provider."""
    while True:
        i = secrets.token_hex(4)
        if i not in taken:
            return i


def _defaults(accts: list[dict[str, Any]], wanted: Any = None) -> dict[str, dict[str, Any]]:
    """The account that serves each area, areas in the order they first appear. A wanted default stands when that account holds
    the area; otherwise the account listed last for the area takes it, which is how the file has always been read."""
    wanted = wanted if isinstance(wanted, dict) else {}
    out: dict[str, dict[str, Any]] = {}
    for area in dict.fromkeys(x for a in accts for x in a.get("areas") or []):
        holders = [a for a in accts if area in (a.get("areas") or [])]
        want = wanted.get(area)
        out[area] = next((a for a in holders if want is not None and a.get("id") == want), holders[-1])
    return out


def _complete(data: dict[str, Any]) -> dict[str, Any]:
    """The accounts as the current layout: version, sign-in kind and a default account for every area filled in."""
    accts = [dict(a, auth=a.get("auth") or DEFAULT_AUTH) for a in data["accounts"]]
    return {"version": FILE_VERSION, "accounts": accts,
            "defaults": {area: a.get("id") for area, a in _defaults(accts, data.get("defaults")).items()},
            "calendars_visible": list(data.get("calendars_visible") or [])}


def normalise(raw: Any) -> dict[str, Any] | None:
    """What a parsed accounts file holds, as the current layout (None when it is not an accounts file). Accounts that cannot work
    (no address, no password, only areas this code does not know) are left out; an account saved with no area at all stays, it
    serves nothing until the owner turns an area on for it. An account without a usable id gets a fresh one. Nothing is written."""
    if not isinstance(raw, dict) or not isinstance(raw.get("accounts"), list):
        return None
    accts = []
    for a in raw["accounts"]:
        if isinstance(a, dict) and a.get("username") and a.get("password") and isinstance(a.get("areas"), list):
            known = [x for x in a["areas"] if x in AREAS]
            if known or not a["areas"]:
                accts.append(dict(_as_known_provider(a), areas=known))
    seen: set[str] = set()
    for a in accts:
        if not isinstance(a.get("id"), str) or not a["id"] or a["id"] in seen:
            a["id"] = new_id(seen)
        seen.add(a["id"])
        if a.get("auth") not in AUTH_KINDS:
            a["auth"] = DEFAULT_AUTH
    version = raw.get("version")
    own = raw.get("defaults") if isinstance(version, int) and version >= 2 else None   # the first layout had no defaults
    visible = raw.get("calendars_visible") or []
    return {"version": FILE_VERSION, "accounts": accts,
            "defaults": {area: a["id"] for area, a in _defaults(accts, own).items()},
            "calendars_visible": [str(u) for u in visible if isinstance(u, str)] if isinstance(visible, list) else []}


def problems(data: dict[str, Any]) -> list[str]:
    """What is wrong with an accounts model, as short notes that name a position and never an address, password or id; [] when it
    is sound: ids unique, every default an account that holds its area, every held area with a default."""
    out: list[str] = []
    if data.get("version") != FILE_VERSION:
        out.append("version is not current")
    accts = data.get("accounts")
    if not isinstance(accts, list) or not all(isinstance(a, dict) for a in accts):
        return out + ["accounts is not a list of accounts"]
    ids: dict[str, dict[str, Any]] = {}
    for n, a in enumerate(accts):
        i = a.get("id")
        if not isinstance(i, str) or not i:
            out.append(f"account {n} has no id")
        elif i in ids:
            out.append(f"account {n} repeats the id of an earlier account")
        else:
            ids[i] = a
        if a.get("auth") not in AUTH_KINDS:
            out.append(f"account {n} has an unknown sign-in kind")
        if not isinstance(a.get("areas"), list) or any(x not in AREAS for x in a["areas"]):
            out.append(f"account {n} has unknown areas")
    defaults = data.get("defaults")
    if not isinstance(defaults, dict):
        return out + ["defaults is missing"]
    for area, i in defaults.items():
        if area not in AREAS:
            out.append(f"a default names an unknown area ({area})")
        elif not isinstance(i, str) or i not in ids:
            out.append(f"the default for {area} is not a known account")
        elif area not in (ids[i].get("areas") or []):
            out.append(f"the default for {area} is an account without that area")
    for area in AREAS:
        if area not in defaults and any(area in (a.get("areas") or []) for a in accts):
            out.append(f"{area} has accounts but no default")
    return out


def load(data_dir: str) -> dict[str, Any] | None:
    """The saved accounts in the current layout, or None when none were saved yet (or the file cannot be read)."""
    try:
        with open(os.path.join(data_dir, ACCOUNTS_FILE)) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    return normalise(raw)


def save(data_dir: str, data: dict[str, Any]) -> None:
    """Write the accounts in the current layout. A model that does not hold together is refused (ValueError), so what is written
    always reads back as it was."""
    model = _complete(data)
    if bad := problems(model):
        raise ValueError("accounts do not hold together: " + "; ".join(bad))
    from .admin import _write_private
    _write_private(os.path.join(data_dir, ACCOUNTS_FILE), json.dumps(model, indent=2, ensure_ascii=False) + "\n")


def put(data: dict[str, Any], acct: dict[str, Any], before_id: str = "") -> dict[str, Any]:
    """The accounts with `acct` saved (it gets its id here). It takes its areas from every other account; one left with no area
    stays connected and serves nothing. An edited account (before_id) keeps its place in the list; a new one goes first. The same sign-in at the
    same provider replaces that account instead of adding a second copy. The deployment's own account ("env") gets a real id once
    it is saved, and the calendar choice is cleared when the calendar comes from another sign-in afterwards."""
    p = acct["provider"]
    same = next((x for x in data["accounts"] if x.get("provider") == p and x["username"].lower() == acct["username"].lower()
                 and x.get("id") != before_id), None)
    replaced = {i for i in (before_id, (same or {}).get("id")) if i}
    acct["id"] = next((i for i in ((same or {}).get("id"), before_id) if i and i != "env"), "") or new_id()
    accts: list[dict[str, Any]] = []
    for x in data["accounts"]:
        if before_id and x.get("id") == before_id:
            accts.append(acct)
        if x.get("id") in replaced:
            continue
        x = dict(x, areas=[a for a in x["areas"] if a not in acct["areas"]])
        accts.append(x if x.get("id") != "env" else dict(x, id=new_id()))
    if not any(x is acct for x in accts):
        accts.insert(0, acct)
    out = dict(data, accounts=accts)
    if (by_area(data).get("calendar") or {}).get("username") != (by_area(out).get("calendar") or {}).get("username"):
        out["calendars_visible"] = []
    return out


def by_area(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The default account of each area that has one, areas in the order they first appear."""
    return _defaults(data["accounts"], data.get("defaults"))


def env_overlay(data_dir: str) -> dict[str, str]:
    """The saved accounts as environment settings ({} without the file): each area from its default account. The Apple Account
    slot (ICLOUD_USERNAME) is the account listed first, whatever it serves."""
    data = load(data_dir)
    if data is None:
        return {}
    if not data["accounts"]:
        return {"ICLOUD_USERNAME": PENDING_USERNAME, "ICLOUD_APP_PASSWORD": "pending-setup", "MAIL_PASSWORD": "",
                "CALDAV_PASSWORD": "", "CARDDAV_PASSWORD": "", "READ_ONLY_AREAS": "", "CALENDARS_VISIBLE": ""}
    areas = by_area(data)
    first = data["accounts"][0]
    env = {"ICLOUD_USERNAME": first["username"], "ICLOUD_APP_PASSWORD": first["password"],
           "ENABLE_MAIL": str("mail" in areas).lower(), "ENABLE_CALENDAR": str("calendar" in areas).lower(),
           "ENABLE_CONTACTS": str("contacts" in areas).lower(),
           "READ_ONLY_AREAS": ",".join(a for a, acct in areas.items() if acct.get("read_only")),
           "CALENDARS_VISIBLE": ",".join(data["calendars_visible"]),
           "MAIL_PASSWORD": "", "CALDAV_PASSWORD": "", "CARDDAV_PASSWORD": ""}
    if m := areas.get("mail"):
        ep = endpoints(m)
        ih, ip = host_port(ep["imap"], 993)
        sh, sp = host_port(ep["smtp"], 587)
        env.update({"IMAP_HOST": ih, "IMAP_PORT": str(ip), "IMAP_SECURITY": security(ip), "IMAP_USERNAME": m["username"],
                    "SMTP_HOST": sh, "SMTP_PORT": str(sp), "SMTP_SECURITY": security(sp), "SMTP_USERNAME": m["username"],
                    "MAIL_PASSWORD": m["password"], "ICLOUD_EMAIL_ADDRESS": m.get("email_address") or m["username"],
                    "ICLOUD_DISPLAY_NAME": m.get("display_name") or ""})
    if c := areas.get("calendar"):
        env.update({"CALDAV_URL": endpoints(c)["caldav"], "CALDAV_USERNAME": c["username"], "CALDAV_PASSWORD": c["password"]})
    if k := areas.get("contacts"):
        env.update({"CARDDAV_URL": endpoints(k)["carddav"], "CARDDAV_USERNAME": k["username"], "CARDDAV_PASSWORD": k["password"]})
    return env


def guess_provider(imap_host: str) -> str:
    return "icloud" if imap_host.lower().endswith("mail.me.com") else "other"


def current(s: Any) -> dict[str, Any]:
    """What the server runs with now, as accounts: the saved file, or the deployment's own single account."""
    data = load(s.data_dir)
    if data is not None:
        return data
    if not s.username or s.username.lower().endswith(".invalid") or not s.app_password:
        return _complete({"accounts": [], "calendars_visible": []})
    provider = guess_provider(s.imap_host)
    acct = {"id": "env", "provider": provider, "username": s.username, "password": s.app_password,
            "areas": [a for a, on in (("mail", s.enable_mail), ("calendar", s.enable_calendar), ("contacts", s.enable_contacts)) if on],
            "read_only": s.read_only, "email_address": "" if s.email_address == s.username else s.email_address,
            "display_name": s.display_name}
    if provider == "other":
        acct.update({"imap": f"{s.imap_host}:{s.imap_port}", "smtp": f"{s.smtp_host}:{s.smtp_port}", "caldav": s.caldav_url,
                     "carddav": s.carddav_url})
    return _complete({"accounts": [acct], "calendars_visible": list(s.calendars_visible)})


# ---------------------------------------------------------------- proving an account works
def trial_settings(s: Any, acct: dict[str, Any]) -> Any:
    """Settings that use only this account, for a sign-in check."""
    ep = endpoints(acct)
    ih, ip = host_port(ep["imap"], 993)
    sh, sp = host_port(ep["smtp"], 587)
    return dataclasses.replace(
        s, username=acct["username"], app_password=acct["password"], mail_password="", caldav_password="", carddav_password="",
        imap_host=ih, imap_port=ip, imap_security=security(ip), imap_username=acct["username"],
        smtp_host=sh, smtp_port=sp, smtp_security=security(sp), smtp_username=acct["username"],
        caldav_url=ep["caldav"], caldav_username=acct["username"], carddav_url=ep["carddav"], carddav_username=acct["username"],
        calendars_visible=(), read_only_areas=(), imap_pool_size=0, caldav_pool_size=1, caldav_keepalive_seconds=0)


class UnknownAccount(LookupError):
    """No saved account has the id that was asked for."""


def settings_for_account(s: Any, account_id: str) -> Any:
    """Settings that run one account, found by its id, inside the deployment's own server settings. Nothing calls it yet: it is
    where an optional `account` argument of the tools will plug in. It starts from trial_settings (this account's sign-in and
    servers, nothing of the others) and puts back what a sign-in check turns off: the connection pools, which areas are read
    only, the From line and the areas the account serves. The calendar filter is one list for the whole file today, so it applies
    to the default calendar account and every other account shows all its calendars. Raises UnknownAccount for an unknown id."""
    data = current(s)
    acct = next((a for a in data["accounts"] if a.get("id") == account_id), None) if isinstance(account_id, str) else None
    if acct is None:
        raise UnknownAccount("no account with this id")
    own = tuple(dict.fromkeys(acct["areas"]))
    visible = data["calendars_visible"] if by_area(data).get("calendar") is acct else []
    return dataclasses.replace(
        trial_settings(s, acct), enable_mail="mail" in own, enable_calendar="calendar" in own, enable_contacts="contacts" in own,
        read_only_areas=own if acct.get("read_only") else (), calendars_visible=tuple(u.rstrip("/") for u in visible),
        email_address=acct.get("email_address") or acct["username"], display_name=acct.get("display_name") or "",
        imap_pool_size=s.imap_pool_size, caldav_pool_size=s.caldav_pool_size, caldav_keepalive_seconds=s.caldav_keepalive_seconds)


def _check_mail(t: Any, send: bool) -> None:
    from .mail import MailService
    svc = MailService(t)
    svc._login().logout()
    if send:
        svc._smtp_open().quit()


def _check_calendar(t: Any) -> list[dict[str, Any]]:
    from .cal import CalendarService
    svc = CalendarService(t)
    try:
        return svc.list_calendars()
    finally:
        svc.close_pool()


def _check_contacts(t: Any) -> None:
    from .contacts import ContactsService
    svc = ContactsService(t)
    with svc._client() as c:
        svc._discover(c)


async def check(s: Any, acct: dict[str, Any], areas: list[str] | None = None) -> dict[str, Any]:
    """Sign in to every area of the account (all at once). Returns {"failed": [areas], "calendars": [...]}; "smtp" in failed
    means reading works but sending was refused. The reasons are not returned: they can quote the account."""
    t = trial_settings(s, acct)
    areas = list(areas if areas is not None else acct["areas"])
    jobs: dict[str, Any] = {}
    if "mail" in areas:
        jobs["mail"] = lambda: _check_mail(t, False)
        if not acct.get("read_only"):
            jobs["smtp"] = lambda: _check_mail(t, True)
    if "calendar" in areas:
        jobs["calendar"] = lambda: _check_calendar(t)
    if "contacts" in areas:
        jobs["contacts"] = lambda: _check_contacts(t)

    kinds: dict[str, str] = {}

    async def run(name: str, fn: Any) -> tuple[str, Any, bool]:
        try:
            return name, await asyncio.wait_for(asyncio.to_thread(fn), CHECK_TIMEOUT), True
        except Exception as e:  # noqa: BLE001 - a check reports failure, it does not raise
            kinds[name] = failure_kind(e)
            log.warning("Account check failed for %s (%s, %s)", name, type(e).__name__, kinds[name])
            return name, None, False

    done = await asyncio.gather(*(run(n, f) for n, f in jobs.items()))
    failed = [n for n, _r, ok in done if not ok]
    if "mail" in failed and "smtp" in failed:
        failed.remove("smtp")
    cals = next((r for n, r, ok in done if n == "calendar" and ok), None) or []
    return {"failed": failed, "kinds": {n: kinds[n] for n in failed}, "calendars": cals}


_REFUSED = ("authenticationfailed", "authentication failed", "authorizationerror", "loginerror", "invalid credentials",
            "smtpauthenticationerror", "unauthorized", "http 401", " 535", "(535")
_UNREACHABLE = ("timed out", "timeout", "connecterror", "connectionerror", "connection refused", "network is unreachable",
                "no route to host", "name or service not known", "temporary failure in name resolution", "nodename nor servname")


def failure_kind(e: BaseException) -> str:
    """Why a sign-in check failed: 'refused' (the provider said no to address or password), 'unreachable' (no answer from
    the provider: network, firewall, DNS) or 'other'. Judged from the exception chain; nothing of it is shown."""
    chain: list[BaseException] = []
    x: BaseException | None = e
    while x is not None and len(chain) < 8:
        chain.append(x)
        x = x.__cause__ or x.__context__
    text = " ".join(f"{type(c).__name__} {c}" for c in chain).lower()
    if any(k in text for k in _REFUSED):
        return "refused"
    if any(isinstance(c, (TimeoutError, ConnectionError)) for c in chain) or any(k in text for k in _UNREACHABLE):
        return "unreachable"
    return "other"
