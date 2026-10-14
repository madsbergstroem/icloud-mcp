"""The accounts a person connects on the /setup page: which provider serves mail, calendar and contacts, with whose sign-in.

They live in DATA_DIR/accounts.json (mode 600). Each area (mail, calendar, contacts) comes from exactly one account, so mail
can be a WEB.DE or Gmail mailbox while calendar and contacts stay in iCloud. env_overlay() turns the file into environment
settings that Settings.from_env reads in place of the deployment's own, so the rest of the server needs no second code path.
Without the file nothing changes. Passwords stay on this server and are never logged or shown.
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
PENDING_USERNAME = "pending@setup.invalid"   # what the server runs with before anyone connected an account
CHECK_TIMEOUT = 25                           # seconds one sign-in check may take

# Server addresses, from each provider's own documentation:
#   iCloud  the upstream project's own defaults (config.py)
#   WEB.DE  hilfe.web.de/pop-imap/imap/imap-serverdaten.html; CalDAV/CardDAV hosts as tested by davx5.com
#   Gmail   developers.google.com/workspace/gmail/imap/imap-smtp; calendar and contacts need Google sign-in (OAuth) instead
PROVIDERS: dict[str, dict[str, Any]] = {
    "icloud": {"label": "iCloud", "imap": "imap.mail.me.com:993", "smtp": "smtp.mail.me.com:587",
               "caldav": "https://caldav.icloud.com", "carddav": "https://contacts.icloud.com", "areas": AREAS},
    "webde": {"label": "WEB.DE", "imap": "imap.web.de:993", "smtp": "smtp.web.de:587",
              "caldav": "https://caldav.web.de", "carddav": "https://carddav.web.de", "areas": AREAS},
    "gmail": {"label": "Gmail", "imap": "imap.gmail.com:993", "smtp": "smtp.gmail.com:465", "caldav": "", "carddav": "",
              "areas": ("mail",)},
    "other": {"label": "Anderer Anbieter", "imap": "", "smtp": "", "caldav": "", "carddav": "", "areas": AREAS},
}


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
            return "", "Ein App-Passwort von Apple hat 16 Buchstaben, etwa abcd-efgh-ijkl-mnop."
        return "-".join(letters[i:i + 4] for i in range(0, 16, 4)).lower(), None
    if provider == "gmail":
        letters = value.replace(" ", "")
        if len(letters) != 16 or not letters.isalpha():
            return "", "Ein App-Passwort von Google hat 16 Buchstaben."
        return letters.lower(), None
    if not value or len(value) > 200:
        return "", "Bitte das Passwort eintragen."
    return value, None


# ---------------------------------------------------------------- the file
def load(data_dir: str) -> dict[str, Any] | None:
    """The saved accounts, or None when the /setup page has not saved any yet (or the file cannot be read)."""
    try:
        with open(os.path.join(data_dir, ACCOUNTS_FILE)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        return None
    accts = [a for a in data["accounts"] if isinstance(a, dict) and a.get("username") and a.get("password")
             and isinstance(a.get("areas"), list)]
    for a in accts:
        a["areas"] = [x for x in a["areas"] if x in AREAS]
    visible = data.get("calendars_visible") or []
    return {"accounts": [a for a in accts if a["areas"]],
            "calendars_visible": [str(u) for u in visible if isinstance(u, str)] if isinstance(visible, list) else []}


def save(data_dir: str, data: dict[str, Any]) -> None:
    from .admin import _write_private
    _write_private(os.path.join(data_dir, ACCOUNTS_FILE), json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def by_area(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {area: a for a in data["accounts"] for area in a["areas"]}


def env_overlay(data_dir: str) -> dict[str, str]:
    """The saved accounts as environment settings ({} without the file)."""
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
    host = imap_host.lower()
    return "icloud" if host.endswith("mail.me.com") else "webde" if host.endswith("web.de") else \
        "gmail" if host.endswith("gmail.com") else "other"


def current(s: Any) -> dict[str, Any]:
    """What the server runs with now, as accounts: the saved file, or the deployment's own single account."""
    data = load(s.data_dir)
    if data is not None:
        return data
    if not s.username or s.username.lower().endswith(".invalid") or not s.app_password:
        return {"accounts": [], "calendars_visible": []}
    provider = guess_provider(s.imap_host)
    acct = {"id": "env", "provider": provider, "username": s.username, "password": s.app_password,
            "areas": [a for a, on in (("mail", s.enable_mail), ("calendar", s.enable_calendar), ("contacts", s.enable_contacts)) if on],
            "read_only": s.read_only, "email_address": "" if s.email_address == s.username else s.email_address,
            "display_name": s.display_name}
    if provider == "other":
        acct.update({"imap": f"{s.imap_host}:{s.imap_port}", "smtp": f"{s.smtp_host}:{s.smtp_port}", "caldav": s.caldav_url,
                     "carddav": s.carddav_url})
    return {"accounts": [acct], "calendars_visible": list(s.calendars_visible)}


def new_id() -> str:
    return secrets.token_hex(4)


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

    async def run(name: str, fn: Any) -> tuple[str, Any, bool]:
        try:
            return name, await asyncio.wait_for(asyncio.to_thread(fn), CHECK_TIMEOUT), True
        except Exception as e:  # noqa: BLE001 - a check reports failure, it does not raise
            log.warning("Account check failed for %s (%s)", name, type(e).__name__)
            return name, None, False

    done = await asyncio.gather(*(run(n, f) for n, f in jobs.items()))
    failed = [n for n, _r, ok in done if not ok]
    if "mail" in failed and "smtp" in failed:
        failed.remove("smtp")
    cals = next((r for n, r, ok in done if n == "calendar" and ok), None) or []
    return {"failed": failed, "calendars": cals}
