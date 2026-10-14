"""/setup: the owner connects their own accounts in the browser and comes back here to change them.

A short wizard (provider, preparation with the provider's own instructions, account and areas, check) and an overview of the
connected accounts: which account serves mail, calendar and contacts, which calendars the assistant may see, read-only per
account, new password, disconnect. Each area comes from one account (accounts.py), so mail can live at WEB.DE or Gmail while
calendar and contacts stay in iCloud.

Only served when owner sign-in through Cloudflare Access is configured (access_sso.py), and only to a request that carries a
verified owner identity: there is no password fallback, because the page exists so that nobody else (an admin, a chat, an
agent) ever handles the person's passwords. Every account is proven with the provider before it is stored in
DATA_DIR/accounts.json; then the server exits so the container restart policy brings it back with the new settings. Passwords
are never logged or shown. The pages are server-rendered without scripts (the page CSP allows none).
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
import re
import secrets
import threading
import time
from typing import Any, Callable
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import access_sso
from . import accounts as A
from .auth import _HEADERS
from .config import Settings

log = logging.getLogger(__name__)

TOKEN_TTL = 1800  # seconds a form stays valid
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_AREA_NAMES = {"mail": "Mail", "calendar": "Kalender", "contacts": "Kontakte"}
_AREA_HINTS = {"mail": "lesen, suchen, sortieren. Senden nur nach deiner Freigabe",
               "calendar": "Termine lesen, anlegen und verschieben", "contacts": "suchen, lesen, Geburtstage"}

# What each provider asks of the person, with the provider's own instructions (sources in accounts.py and below).
#   Apple   support.apple.com/de-de/102654 (app-specific passwords, two-factor authentication required)
#   WEB.DE  hilfe.web.de/pop-imap/einschalten.html, hilfe.web.de/sicherheit/2fa/anwendungsspezifisches-passwort.html
#   Google  support.google.com/accounts/answer/185833 (app passwords), support.google.com/mail/answer/7126229 (IMAP always on)
_APPLE_PW = "https://account.apple.com/account/manage/section/security"
_GOOGLE_PW = "https://myaccount.google.com/apppasswords"
_WEBDE_PW = "https://hilfe.web.de/sicherheit/2fa/anwendungsspezifisches-passwort.html"
_PASSWORD_HOME = {"icloud": ("bei Apple", _APPLE_PW), "gmail": ("bei Google", _GOOGLE_PW), "webde": ("bei WEB.DE", _WEBDE_PW)}

_CSS = """
:root{--bg:#f2f3f5;--card:#fff;--fg:#1b1d22;--muted:#5d6370;--line:#dcdfe5;--accent:#2463d1;--accent-fg:#fff;--soft:#eaf0fa;
--ok:#1b7a3c;--ok-soft:#e5f4ea;--err:#b42318;--err-soft:#fdecea;--note:#7a5b00;--note-soft:#fff6db;
--font:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",system-ui,sans-serif;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0e1014;--card:#181b21;--fg:#eceef2;--muted:#a2a8b4;--line:#2c313a;--accent:#5c93f0;
--accent-fg:#0e1014;--soft:#1a2535;--ok:#4cc27a;--ok-soft:#14281c;--err:#ff8a80;--err-soft:#33191a;--note:#f0c44d;--note-soft:#2c2614;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 var(--font);padding-inline:16px;padding-block:20px}
.wrap{max-width:30rem;margin:0 auto;display:grid;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px}
.who{font-size:.84rem;color:var(--muted);margin:0 0 12px}
.progress{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:0 0 6px}
.progress i{height:4px;border-radius:4px;background:var(--line)} .progress i.on{background:var(--accent)}
.stepname{font-size:.82rem;color:var(--muted);margin:0 0 14px}
h1{font-size:1.4rem;line-height:1.25;margin:0 0 8px;text-wrap:balance} h2{font-size:1.05rem;margin:22px 0 4px}
p{margin:.45rem 0} .muted{color:var(--muted);font-size:.93rem} .small{font-size:.84rem;color:var(--muted)}
.box{border-radius:12px;padding:12px 14px;font-size:.92rem;margin:14px 0}
.note{background:var(--note-soft)} .note strong{color:var(--note)} .trust{background:var(--soft)}
.okbox{background:var(--ok-soft)} .okbox strong{color:var(--ok)} .errbox{background:var(--err-soft)} .errbox strong{color:var(--err)}
ol.how{margin:10px 0;padding-left:1.3rem;display:grid;gap:6px} ol.how b{font-weight:600}
code{font-family:var(--mono);font-size:.92em;background:var(--soft);padding:1px 6px;border-radius:6px}
.actions{display:grid;gap:10px;margin-top:18px}
.btn{display:block;width:100%;text-align:center;background:var(--accent);color:var(--accent-fg);border:0;border-radius:12px;padding:13px 16px;
font:600 1rem var(--font);text-decoration:none;cursor:pointer}
.btn.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)} .btn.danger{color:var(--err)}
.btn:focus-visible,input:focus-visible,summary:focus-visible,a:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
label{display:block;font-weight:600;margin:16px 0 6px} .hint{display:block;font-weight:400;color:var(--muted);font-size:.85rem}
input[type=email],input[type=text],input[type=password],input[type=url]{width:100%;padding:12px 13px;font:1rem var(--font);
border:1px solid var(--line);border-radius:11px;background:var(--card);color:var(--fg)}
input.pw{font-family:var(--mono);letter-spacing:.04em}
details{margin-top:16px;border-top:1px solid var(--line);padding-top:12px} summary{cursor:pointer;font-weight:600;color:var(--accent)}
fieldset{border:1px solid var(--line);border-radius:12px;padding:6px 14px 12px;margin:18px 0 0} legend{font-weight:600;padding:0 6px}
label.opt{display:grid;grid-template-columns:22px minmax(0,1fr);gap:10px;align-items:start;font-weight:400;margin:10px 0 0;cursor:pointer}
label.opt input{width:20px;height:20px;margin:2px 0 0;accent-color:var(--accent)} label.opt b{display:block}
.providers{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,12rem),1fr));gap:10px;margin-top:16px}
.prov{display:grid;gap:2px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px 14px;color:var(--fg);text-decoration:none}
.prov span{font-size:.85rem;color:var(--muted)} .prov:hover{border-color:var(--accent)}
.accounts{display:grid;gap:8px;margin:14px 0}
.acct{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 10px;border:1px solid var(--line);border-radius:12px;padding:11px 13px}
.acct span{font-size:.86rem;color:var(--muted);overflow-wrap:anywhere} .acct .st{grid-column:2;grid-row:1;font-weight:600}
.acct span.good{color:var(--ok)} .acct span.bad{color:var(--err)}
dl.facts{margin:14px 0;border:1px solid var(--line);border-radius:12px;overflow:hidden}
dl.facts div{display:grid;grid-template-columns:minmax(0,9rem) minmax(0,1fr);gap:10px;padding:10px 13px;border-top:1px solid var(--line)}
dl.facts div:first-child{border-top:0} dt{color:var(--muted);font-size:.9rem} dd{margin:0;overflow-wrap:anywhere}
.good{color:var(--ok);font-weight:600} .bad{color:var(--err);font-weight:600} .off{color:var(--muted)}
a.inl{color:var(--accent);font-weight:600;text-decoration:none}
"""


def _e(v: Any) -> str:
    return html.escape(str(v), quote=True)


def _html(body: str, status: int = 200) -> Response:
    page = (f'<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="color-scheme" content="light dark"><title>Konten verbinden</title><style>{_CSS}</style></head>'
            f'<body><div class="wrap"><main class="card">{body}</main></div></body></html>')
    return HTMLResponse(page, status_code=status, headers=_HEADERS)


def _ext(href: str, text: str, cls: str = "inl") -> str:
    return f'<a class="{cls}" href="{_e(href)}" target="_blank" rel="noopener noreferrer">{text} ↗</a>'


def _progress(step: int, name: str) -> str:
    bars = "".join(f'<i{" class=on" if i < step else ""}></i>' for i in range(4))
    return f'<div class="progress">{bars}</div><p class="stepname">{name}</p>'


def _areas_text(areas: list[str]) -> str:
    names = [_AREA_NAMES[a] for a in A.AREAS if a in areas]
    return " und ".join([", ".join(names[:-1]), names[-1]]) if len(names) > 1 else "".join(names)


def _label(acct: dict[str, Any]) -> str:
    return A.PROVIDERS.get(acct.get("provider", ""), A.PROVIDERS["other"])["label"]


# ---------------------------------------------------------------- step 2: preparation, per provider
def _prepare(provider: str) -> str:
    nxt = f'/setup?{urlencode({"anbieter": provider, "schritt": "eintragen"})}'
    back = '<a class="btn ghost" href="/setup?neu=1">Anderen Anbieter wählen</a>'
    if provider == "icloud":
        return f"""{_progress(2, "Schritt 2 von 4 · Vorbereiten")}<h1>iCloud mit deinem KI-Assistenten verbinden</h1>
<p>Danach kann dein KI-Assistent die Bereiche nutzen, die du auswählst: Mail, Kalender, Kontakte. Mails verschickt er nur, wenn du sie vorher freigibst.</p>
<p class="muted">Dauer: etwa 3 Minuten. Du brauchst dein iPhone oder deinen Mac zur Bestätigung bei Apple.</p>
<div class="box note"><strong>Vorab:</strong> Apple vergibt App-Passwörter nur mit Zwei-Faktor-Authentifizierung. Wenn du dich bei Apple mit Code auf dem iPhone anmeldest, ist sie an.</div>
<p><b>So holst du das Passwort bei Apple:</b> <span class="small">({_ext("https://support.apple.com/de-de/102654", "Anleitung von Apple")})</span></p>
<ol class="how"><li>Unten auf <b>Apple-Account öffnen</b> tippen und anmelden.</li>
<li><b>Anmeldung und Sicherheit</b> → <b>App-spezifische Passwörter</b>.</li>
<li><b>App-spezifisches Passwort generieren</b>, als Name zum Beispiel „KI-Assistent“ eintragen.</li>
<li>Apple zeigt ein Passwort wie <code>abcd-efgh-ijkl-mnop</code>. Kopieren und hierher zurückkommen.</li></ol>
<div class="actions">{_ext(_APPLE_PW, "Apple-Account öffnen", "btn")}<a class="btn ghost" href="{nxt}">Ich habe das Passwort kopiert</a>{back}</div>"""
    if provider == "webde":
        return f"""{_progress(2, "Schritt 2 von 4 · Vorbereiten")}<h1>WEB.DE-Postfach freischalten</h1>
<p>Zwei kurze Einstellungen bei WEB.DE, am besten am Computer. Danach kann dein Assistent Mail, Kalender und Kontakte nutzen.</p>
<p><b>1. Zugriff erlauben</b> <span class="small">({_ext("https://hilfe.web.de/pop-imap/einschalten.html", "Anleitung von WEB.DE")})</span></p>
<ol class="how"><li>Auf web.de anmelden, oben auf deine <b>Initialen</b> klicken → <b>E-Mail-Einstellungen</b>.</li>
<li>Unter „E-Mail empfangen“ → <b>POP3/IMAP</b>.</li>
<li>Den Schieberegler auf <b>POP3- und IMAP-Zugriff erlauben</b> stellen und die Sicherheitsfrage lösen.</li></ol>
<p><b>2. App-Passwort erstellen</b> <span class="small">({_ext(_WEBDE_PW, "Anleitung von WEB.DE")})</span></p>
<ol class="how"><li>Initialen → <b>Account verwalten</b> → links <b>Login &amp; Sicherheit</b>.</li>
<li><b>App-Passwörter (für externe Apps)</b> → <b>Neues App-Passwort erstellen</b>, als Name zum Beispiel „KI-Assistent“.</li>
<li>Passwort sofort kopieren, WEB.DE zeigt es nur einmal.</li></ol>
<div class="box note"><strong>Gut zu wissen:</strong> Wird der Zugang längere Zeit nicht genutzt, schaltet WEB.DE ihn aus Sicherheitsgründen wieder ab. Dann hier in der Übersicht neu verbinden.</div>
<div class="actions">{_ext("https://web.de", "WEB.DE öffnen", "btn")}<a class="btn ghost" href="{nxt}">Erledigt, weiter</a>{back}</div>"""
    if provider == "gmail":
        return f"""{_progress(2, "Schritt 2 von 4 · Vorbereiten")}<h1>Gmail verbinden</h1>
<p>Bei Gmail geht nur Mail. Kalender und Kontakte erlaubt Google ausschließlich über eine Google-Anmeldung, die diese Seite nicht anbietet.</p>
<div class="box note"><strong>Vorab:</strong> Nur private Google-Konten. Die 2‑Faktor-Authentifizierung muss an sein. Bei Firmen- oder Schulkonten bietet Google keine App-Passwörter an.</div>
<p><b>App-Passwort erstellen</b> <span class="small">({_ext("https://support.google.com/accounts/answer/185833?hl=de", "Anleitung von Google")})</span></p>
<ol class="how"><li>Unten auf <b>App-Passwörter öffnen</b> tippen und bei Google anmelden.</li>
<li>Einen Namen eingeben, zum Beispiel „KI-Assistent“, und erstellen.</li>
<li>Das Passwort kopieren und hierher zurückkommen.</li></ol>
<p class="small">IMAP ist bei Gmail immer eingeschaltet, dort musst du nichts ändern.</p>
<div class="actions">{_ext(_GOOGLE_PW, "App-Passwörter öffnen", "btn")}<a class="btn ghost" href="{nxt}">Ich habe das Passwort kopiert</a>{back}</div>"""
    return f"""{_progress(2, "Schritt 2 von 4 · Server")}<h1>Server deines Anbieters</h1>
<p class="muted">Die Angaben findest du in der Hilfe deines Anbieters unter „IMAP“ oder „Mail-Programm einrichten“. Was du nicht brauchst, bleibt leer.</p>
<form method="get" action="/setup"><input type="hidden" name="anbieter" value="other"><input type="hidden" name="schritt" value="eintragen">
<label for="o-imap">Mail empfangen (IMAP)</label><input id="o-imap" name="imap" type="text" placeholder="imap.anbieter.de:993" autocomplete="off">
<label for="o-smtp">Mail senden (SMTP)</label><input id="o-smtp" name="smtp" type="text" placeholder="smtp.anbieter.de:587" autocomplete="off">
<label for="o-cal">Kalender (CalDAV)</label><input id="o-cal" name="caldav" type="url" placeholder="https://dav.anbieter.de/" autocomplete="off">
<label for="o-con">Kontakte (CardDAV)</label><input id="o-con" name="carddav" type="url" placeholder="https://dav.anbieter.de/" autocomplete="off">
<div class="actions"><button class="btn" type="submit">Weiter</button>{back}</div></form>"""


def _server_problem(ep: dict[str, str]) -> str | None:
    if not (ep["imap"] or ep["smtp"] or ep["caldav"] or ep["carddav"]):
        return "Bitte mindestens einen Server eintragen."
    if bool(ep["imap"]) != bool(ep["smtp"]):
        return "Für Mail braucht es beide Server: IMAP zum Empfangen und SMTP zum Senden."
    for key, port in (("imap", 993), ("smtp", 587)):
        if ep[key] and not A.host_port(ep[key], port)[0]:
            return f"„{ep[key]}“ ist keine Serveradresse. Beispiel: {key}.anbieter.de:{port}"
    for key in ("caldav", "carddav"):
        if ep[key] and not re.fullmatch(r"https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(:\d{2,5})?(/\S*)?", ep[key]):
            return f"„{ep[key]}“ ist keine https-Adresse. Beispiel: https://dav.anbieter.de/"
    return None


# ---------------------------------------------------------------- what went wrong, per provider
def _failure(acct: dict[str, Any], failed: list[str]) -> str:
    p = acct.get("provider")
    who = {"icloud": "Apple", "webde": "WEB.DE", "gmail": "Google"}.get(p, "Dein Anbieter")
    if failed == ["smtp"]:
        head = f"{who} lässt das Lesen zu, aber nicht das Senden."
    elif "mail" not in failed and "smtp" not in failed:
        head = f"Die Anmeldung für {_areas_text(failed)} hat nicht geklappt."
    else:
        head = f"{who} hat die Anmeldung abgelehnt."
    reasons = {
        "icloud": ["Die Adresse ist nicht die, mit der du dich bei Apple anmeldest.",
                   "Das Passwort wurde nicht vollständig kopiert. Es hat 16 Buchstaben.",
                   "Es wurde ein altes Passwort verwendet. Wenn du dein Apple-Passwort geändert hast, löscht Apple alle App-Passwörter."],
        "webde": ["Der POP3- und IMAP-Zugriff ist bei WEB.DE noch nicht erlaubt oder wurde nach langer Pause wieder abgeschaltet.",
                  "Das App-Passwort wurde nicht vollständig kopiert. WEB.DE zeigt es nur einmal, im Zweifel ein neues erstellen.",
                  "Die Adresse ist nicht deine vollständige WEB.DE-Adresse."],
        "gmail": ["Das App-Passwort wurde nicht vollständig kopiert. Es hat 16 Buchstaben.",
                  "Die 2‑Faktor-Authentifizierung ist aus. Ohne sie gibt es keine App-Passwörter.",
                  "Es ist ein Firmen- oder Schulkonto. Dort lässt Google keine App-Passwörter zu."],
    }.get(p, ["Die Serveradressen stimmen nicht. Sie stehen in der Hilfe deines Anbieters.",
              "Benutzername oder Passwort stimmen nicht.",
              "Dein Anbieter verlangt ein eigenes App-Passwort für Mail-Programme."])
    items = "".join(f"<li>{_e(r)}</li>" for r in reasons)
    return (f'<div class="box errbox"><strong>{_e(head)}</strong> Es wurde nichts gespeichert.</div>'
            f'<p><b>Meistens liegt es an einem dieser Punkte:</b></p><ol class="how">{items}</ol>')


# ---------------------------------------------------------------- the routes
def register_setup_routes(mcp: Any, settings: Settings, *, provider: Any = None,
                          exit_fn: Callable[[], None] | None = None) -> None:
    access = access_sso.from_settings(settings)
    if access is None:
        return  # without a verified owner identity there is no safe way to offer this page
    key = secrets.token_bytes(32)  # per-process: a form issued before a restart simply asks again
    exit_fn = exit_fn or (lambda: os._exit(0))  # the container restart policy starts the server again

    def _tok(who: str, exp: int) -> str:
        return hmac.new(key, f"setup|{who}|{exp}".encode(), hashlib.sha256).hexdigest()

    def _hidden(who: str) -> str:
        exp = int(time.time()) + TOKEN_TTL
        return f'<input type="hidden" name="exp" value="{exp}"><input type="hidden" name="tok" value="{_tok(who, exp)}">'

    def _who(who: str) -> str:
        return f'<p class="who">Angemeldet als {_e(who)}</p>'

    def _restart() -> None:
        threading.Timer(1.0, exit_fn).start()  # after the response has gone out

    def _data() -> dict[str, Any]:
        return A.current(settings)

    # ------------------------------------------------------------ pages
    def providers_page(who: str, data: dict[str, Any]) -> Response:
        tiles = "".join(f'<a class="prov" href="/setup?anbieter={p}"><b>{_e(A.PROVIDERS[p]["label"])}</b><span>{d}</span></a>'
                        for p, d in (("icloud", "Mail, Kalender, Kontakte"), ("webde", "Mail, Kalender, Kontakte"),
                                     ("gmail", "nur Mail"), ("other", "IMAP, SMTP, CalDAV, CardDAV")))
        back = '<div class="actions"><a class="btn ghost" href="/setup">Zurück zur Übersicht</a></div>' if data["accounts"] else ""
        return _html(f"""{_who(who)}{_progress(1, "Schritt 1 von 4 · Anbieter")}<h1>Konto mit deinem KI-Assistenten verbinden</h1>
<p>Wo liegen deine Mails, Termine oder Kontakte? Du kannst später weitere Konten hinzufügen, zum Beispiel privat und beruflich.</p>
<div class="providers">{tiles}</div>{back}""")

    def form_page(who: str, data: dict[str, Any], acct: dict[str, Any], *, editing: bool, error: str = "",
                  failure: str = "", status: int = 200) -> Response:
        p = acct["provider"]
        possible = A.possible_areas(acct)
        taken = {area: a for area, a in A.by_area(data).items() if a.get("id") != acct.get("id")}
        chosen = acct.get("areas") if acct.get("areas") is not None else [a for a in possible if a not in taken]
        boxes = ""
        for area in possible:
            other = taken.get(area)
            note = (f'<span class="hint">kommt bisher von {_e(_label(other))} ({_e(other["username"])}). '
                    f'Wählst du es hier, wechselt es zu diesem Konto.</span>') if other else ""
            boxes += (f'<label class="opt"><input type="checkbox" name="area" value="{area}"{" checked" if area in chosen else ""}>'
                      f'<span><b>{_AREA_NAMES[area]}</b><span class="hint">{_AREA_HINTS[area]}</span>{note}</span></label>')
        boxes += (f'<label class="opt"><input type="checkbox" name="read_only" value="1"{" checked" if acct.get("read_only") else ""}>'
                  f'<span><b>Nur lesen</b><span class="hint">Dein Assistent kann nachsehen, aber nichts anlegen, ändern oder senden.</span></span></label>')
        ep_fields = "".join(f'<input type="hidden" name="{k}" value="{_e(v)}">' for k, v in A.endpoints(acct).items()) if p == "other" else ""
        pw_home = _PASSWORD_HOME.get(p)
        account_hint = {"icloud": "die Adresse deines Apple-Accounts", "webde": "deine vollständige WEB.DE-Adresse",
                        "gmail": "deine Gmail-Adresse"}.get(p, "meist deine E-Mail-Adresse")
        pw_hint = ("aus dem vorigen Schritt, einfach einfügen" if pw_home else "das Passwort deines Postfachs oder ein App-Passwort") + \
                  (f'. Noch keins? {_ext(pw_home[1], pw_home[0].replace("bei ", "Bei ") + " erstellen")}' if pw_home else "")
        if editing:
            pw_hint = "leer lassen, dann bleibt das bisherige Passwort"
        placeholder = {"icloud": "abcd-efgh-ijkl-mnop", "gmail": "abcd efgh ijkl mnop"}.get(p, "")
        sender = ""
        if "mail" in possible:
            sender = f"""<details{" open" if acct.get("email_address") or acct.get("display_name") else ""}><summary>Optional: Absender und Name</summary>
<label for="from">Mails senden als<span class="hint">leer lassen, dann gilt die Adresse oben</span></label>
<input id="from" name="email_address" type="email" autocomplete="email" value="{_e(acct.get("email_address", ""))}">
<label for="name">Dein Name in Mails</label>
<input id="name" name="display_name" type="text" autocomplete="name" maxlength="200" value="{_e(acct.get("display_name", ""))}"></details>"""
        trust = "Das Passwort wird nur auf diesem Server gespeichert und nirgends angezeigt. Dein KI-Assistent bekommt es nie zu sehen."
        if pw_home:
            trust += f" Löschen kannst du es jederzeit {pw_home[0]}: {_ext(pw_home[1], 'App-Passwörter')}."
        back = "/setup" if editing else f'/setup?anbieter={p}'
        err = f'<div class="box errbox"><strong>{_e(error)}</strong></div>' if error else ""
        return _html(f"""{_who(who)}{_progress(3, "Schritt 3 von 4 · Eintragen")}<h1>{_e(_label(acct))}: Konto und Passwort</h1>{failure}{err}
<form method="post" action="/setup">{_hidden(who)}<input type="hidden" name="action" value="connect">
<input type="hidden" name="anbieter" value="{_e(p)}"><input type="hidden" name="konto" value="{_e(acct.get("id", ""))}">{ep_fields}
<label for="acc">E-Mail-Adresse des Kontos<span class="hint">{account_hint}</span></label>
<input id="acc" name="username" type="email" autocomplete="username" required value="{_e(acct.get("username", ""))}">
<label for="pw">{"Neues " if editing else ""}App-Passwort<span class="hint">{pw_hint}</span></label>
<input id="pw" class="pw" name="password" type="password" autocomplete="off" spellcheck="false" placeholder="{placeholder}"{"" if editing else " required"}>
<fieldset><legend>Was darf dein Assistent nutzen?</legend>{boxes}</fieldset>{sender}
<div class="box trust">{trust}</div>
<div class="actions"><button class="btn" type="submit">Verbindung prüfen</button><a class="btn ghost" href="{back}">Zurück</a></div></form>
<p class="small">Die Prüfung meldet sich einmal beim Anbieter an und dauert ein paar Sekunden.</p>""", status)

    async def overview_page(who: str, data: dict[str, Any], notice: str = "") -> Response:
        import asyncio
        accts = data["accounts"]
        if not accts:
            return providers_page(who, data)
        checks = await asyncio.gather(*(A.check(settings, a) for a in accts))
        tiles, sections = "", ""
        for a, chk in zip(accts, checks):
            ok = not chk["failed"]
            tiles += (f'<div class="acct"><b>{_e(_label(a))}</b><span>{_e(a["username"])} · {_areas_text(a["areas"])}</span>'
                      f'<span class="st {"good" if ok else "bad"}">{"verbunden" if ok else "Problem"}</span></div>')
            rows = [("Konto", _e(a["username"]))]
            if "mail" in a["areas"]:
                rows += [("Mails senden als", _e(a.get("email_address") or a["username"])),
                         ("Name in Mails", _e(a.get("display_name") or "–"))]
            for area in A.possible_areas(a):
                if area in a["areas"]:
                    bad = area in chk["failed"] or (area == "mail" and "smtp" in chk["failed"])
                    state = '<span class="bad">an · Anmeldung klappt nicht</span>' if bad else '<span class="good">an · erreichbar</span>'
                else:
                    state = '<span class="off">aus</span>'
                rows.append((_AREA_NAMES[area], state))
            rows.append(("Rechte", "nur lesen" if a.get("read_only") else "lesen und ändern"))
            facts = "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in rows)
            edit = f'/setup?{urlencode({"anbieter": a["provider"], "schritt": "eintragen", "konto": a["id"]})}'
            pw_home = _PASSWORD_HOME.get(a["provider"])
            manage = _ext(pw_home[1], f"App-Passwörter {pw_home[0]} verwalten", "btn ghost") if pw_home else ""
            cal = calendars_block(who, data, a, chk) if "calendar" in a["areas"] else ""
            sections += f"""<h2>{_e(_label(a))} · {_e(a["username"])}</h2>{"" if ok else _failure(a, chk["failed"])}
<dl class="facts">{facts}</dl>{cal}
<div class="actions"><a class="btn ghost" href="{edit}">Passwort, Bereiche oder Absender ändern</a>{manage}
<a class="btn ghost danger" href="/setup?{urlencode({"trennen": a["id"]})}">Dieses Konto trennen</a></div>"""
        outbox = ""
        if settings.require_approval and any("mail" in a["areas"] and not a.get("read_only") for a in accts):
            outbox = '<p>Mails, die auf deine Freigabe warten: <a class="inl" href="/outbox">Freigaben ansehen</a></p>'
        apps = ""
        if provider is not None:
            n = provider.connected_clients()
            apps = f"""<details><summary>Verbundene Apps</summary><p class="muted">Gerade verbunden: {n} {"App" if n == 1 else "Apps"}.
Abmelden heißt: Jede App muss sich neu anmelden, die Konten bleiben verbunden.</p>
<form method="post" action="/setup">{_hidden(who)}<input type="hidden" name="action" value="signout">
<div class="actions"><button class="btn ghost" type="submit">Alle verbundenen Apps abmelden</button></div></form></details>"""
        note = f'<div class="box okbox"><strong>{_e(notice)}</strong></div>' if notice else ""
        return _html(f"""{_who(who)}<h1>Deine verbundenen Konten</h1>{note}<div class="accounts">{tiles}
<a class="btn ghost" href="/setup?neu=1">+ Konto hinzufügen</a></div>{outbox}{sections}
<p class="small">Nach einer Änderung startet der Dienst neu und ist nach etwa 30 Sekunden wieder bereit. Wurden Bereiche ein- oder
ausgeschaltet, verbinde deinen Assistenten danach einmal neu, damit er die neuen Funktionen kennt.</p>{apps}""")

    def calendars_block(who: str, data: dict[str, Any], a: dict[str, Any], chk: dict[str, Any]) -> str:
        from .cal import calendar_path
        cals = chk["calendars"]
        if not cals:
            return ""
        visible = {calendar_path(u) for u in data["calendars_visible"]}
        on = [c for c in cals if not visible or calendar_path(c["id"]) in visible]
        boxes = "".join(f'<label class="opt"><input type="checkbox" name="cal" value="{_e(c["id"])}"'
                        f'{" checked" if c in on else ""}><span><b>{_e(c["name"])}</b></span></label>' for c in cals)
        return f"""<details><summary>Welche Kalender darf er sehen? ({len(on)} von {len(cals)})</summary>
<p class="muted">Nicht ausgewählte Kalender sind für deinen Assistenten unsichtbar, auch bei „freie Zeit finden“.</p>
<form method="post" action="/setup">{_hidden(who)}<input type="hidden" name="action" value="calendars">{boxes}
<div class="actions"><button class="btn" type="submit">Auswahl speichern</button></div></form></details>"""

    def remove_page(who: str, a: dict[str, Any]) -> Response:
        pw_home = _PASSWORD_HOME.get(a["provider"])
        extra = f" Das App-Passwort löschst du zusätzlich {pw_home[0]}: {_ext(pw_home[1], 'App-Passwörter')}." if pw_home else ""
        return _html(f"""{_who(who)}<h1>{_e(_label(a))}-Konto trennen?</h1>
<p>Dein Assistent kann danach nicht mehr auf {_areas_text(a["areas"])} von <b>{_e(a["username"])}</b> zugreifen.
Adresse und Passwort werden von diesem Server gelöscht.{extra}</p>
<form method="post" action="/setup">{_hidden(who)}<input type="hidden" name="action" value="remove"><input type="hidden" name="konto" value="{_e(a["id"])}">
<div class="actions"><button class="btn danger ghost" type="submit">Ja, trennen</button><a class="btn ghost" href="/setup">Abbrechen</a></div></form>""")

    def done_page(who: str, a: dict[str, Any]) -> Response:
        tries = []
        if "calendar" in a["areas"]:
            tries.append("„Was steht morgen in meinem Kalender?“")
        if "mail" in a["areas"]:
            tries.append("„Habe ich heute wichtige Mails bekommen?“")
        if "contacts" in a["areas"]:
            tries.append("„Wer hat diesen Monat Geburtstag?“")
        items = "".join(f"<li>{t}</li>" for t in tries)
        return _html(f"""{_who(who)}{_progress(4, "Fertig")}<div class="box okbox"><strong>Verbunden.</strong> Der Dienst startet neu und ist in etwa 30 Sekunden bereit.</div>
<h1>Dein Assistent ist mit {_e(_label(a))} verbunden</h1><p>Probier es aus und frag deinen Assistenten zum Beispiel:</p><ol class="how">{items}</ol>
<p class="small">Will dein Assistent eine Mail verschicken, bekommst du vorher einen Link zur Freigabe. Ändern kannst du alles später unter derselben Adresse.</p>
<div class="actions"><a class="btn ghost" href="/setup">Zur Übersicht</a></div>""")

    def find(data: dict[str, Any], acct_id: str) -> dict[str, Any] | None:
        return next((a for a in data["accounts"] if a.get("id") == acct_id), None) if acct_id else None

    def draft(q: Any, data: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        """The account a step-3 form is about: an existing one (editing) or a new one for the chosen provider."""
        existing = find(data, str(q.get("konto", "")))
        if existing is not None:
            return dict(existing), True
        p = str(q.get("anbieter", ""))
        if p not in A.PROVIDERS:
            return None, False
        acct: dict[str, Any] = {"id": "", "provider": p, "username": "", "areas": None}
        if p == "other":
            acct.update({k: str(q.get(k, "")).strip() for k in ("imap", "smtp", "caldav", "carddav")})
        return acct, False

    # ------------------------------------------------------------ handlers
    async def _identity(request: Request) -> str | None:
        return await access.owner_email(request.headers)

    def _refused() -> Response:
        return _html('<p class="bad">Diese Seite ist nur nach der Anmeldung über Cloudflare Access erreichbar.</p>', 403)

    @mcp.custom_route("/setup", methods=["GET"])
    async def setup_get(request: Request) -> Response:
        who = await _identity(request)
        if not who:
            return _refused()
        q = request.query_params
        data = _data()
        if q.get("neu"):
            return providers_page(who, data)
        if (rid := q.get("trennen")) and (a := find(data, rid)):
            return remove_page(who, a)
        if q.get("anbieter") or q.get("konto"):
            acct, editing = draft(q, data)
            if acct is None:
                return providers_page(who, data)
            if q.get("schritt") == "eintragen":
                if acct["provider"] == "other" and not editing and (problem := _server_problem(A.endpoints(acct))):
                    return _html(f'{_who(who)}<div class="box errbox"><strong>{_e(problem)}</strong></div>{_prepare("other")}', 400)
                return form_page(who, data, acct, editing=editing)
            return _html(_who(who) + _prepare(acct["provider"]))
        return await overview_page(who, data)

    @mcp.custom_route("/setup", methods=["POST"])
    async def setup_post(request: Request) -> Response:
        who = await _identity(request)
        if not who:
            return _refused()
        form = await request.form()
        try:
            exp = int(str(form.get("exp", "0")))
        except ValueError:
            exp = 0
        if exp < time.time() or not hmac.compare_digest(str(form.get("tok", "")).encode(), _tok(who, exp).encode()):
            log.warning("Rejected a /setup submission with an expired or invalid token")
            return _html(f'{_who(who)}<div class="box errbox"><strong>Die Seite war zu lange offen.</strong> Bitte noch einmal von vorn.</div>'
                         '<div class="actions"><a class="btn" href="/setup">Zur Übersicht</a></div>', 403)
        data = _data()
        action = str(form.get("action", ""))
        if action == "signout" and provider is not None:
            n = provider.sign_out_all()
            log.warning("Owner signed out all connected apps on /setup")
            return await overview_page(who, data, f"{n} {'App' if n == 1 else 'Apps'} abgemeldet.")
        if action == "calendars":
            data["calendars_visible"] = [str(c) for c in form.getlist("cal") if str(c).startswith("https://")]
            A.save(settings.data_dir, data)
            log.warning("Owner changed the visible calendars on /setup; restarting")
            _restart()
            return _html(f'{_who(who)}<div class="box okbox"><strong>Gespeichert.</strong> Der Dienst startet neu und ist in etwa 30 Sekunden bereit.</div>'
                         '<div class="actions"><a class="btn ghost" href="/setup">Zur Übersicht</a></div>')
        if action == "remove" and (a := find(data, str(form.get("konto", "")))):
            data["accounts"] = [x for x in data["accounts"] if x is not a]
            if "calendar" in a["areas"]:
                data["calendars_visible"] = []
            A.save(settings.data_dir, data)
            log.warning("Owner disconnected an account on /setup; restarting")
            _restart()
            return _html(f'{_who(who)}<div class="box okbox"><strong>Getrennt.</strong> Der Dienst startet neu und ist in etwa 30 Sekunden bereit.</div>'
                         '<div class="actions"><a class="btn ghost" href="/setup">Zur Übersicht</a></div>')
        if action != "connect":
            return await overview_page(who, data)
        return await connect(who, data, form)

    async def connect(who: str, data: dict[str, Any], form: Any) -> Response:
        acct, editing = draft(form, data)
        if acct is None:
            return providers_page(who, data)
        before = dict(acct)
        p = acct["provider"]
        acct["username"] = str(form.get("username", "")).strip()
        acct["areas"] = [a for a in A.AREAS if a in form.getlist("area") and a in A.possible_areas(acct)]
        acct["read_only"] = form.get("read_only") == "1"
        acct["email_address"] = str(form.get("email_address", "")).strip()[:254]
        acct["display_name"] = str(form.get("display_name", "")).strip()[:200]
        raw = str(form.get("password", ""))

        def again(error: str = "", failure: str = "", status: int = 400) -> Response:
            return form_page(who, data, acct, editing=editing, error=error, failure=failure, status=status)

        if p == "other" and (problem := _server_problem(A.endpoints(acct))):
            return again(problem)
        if not _EMAIL.fullmatch(acct["username"]) or len(acct["username"]) > 254:
            return again("Das sieht nicht wie eine E-Mail-Adresse aus.")
        if acct["email_address"] and not _EMAIL.fullmatch(acct["email_address"]):
            return again("„Mails senden als“ ist keine E-Mail-Adresse.")
        if not acct["areas"]:
            return again("Mindestens einen Bereich auswählen.")
        if raw.strip() or not editing:
            acct["password"], problem = A.normalise_password(p, raw)
            if problem:
                return again(problem)
        result = await A.check(settings, acct)
        if result["failed"]:
            return again(failure=_failure(acct, result["failed"]))
        # Saved: this account takes over its areas from any other account; an account left with no area is dropped.
        # The same login at the same provider again replaces that account instead of adding a second copy.
        same = next((x for x in data["accounts"] if x.get("provider") == p and x["username"].lower() == acct["username"].lower()
                     and x.get("id") != before.get("id")), None)
        replaced = {i for i in (before.get("id"), (same or {}).get("id")) if i}
        acct["id"] = next((i for i in ((same or {}).get("id"), before.get("id")) if i and i != "env"), "") or A.new_id()
        old_cal = A.by_area(data).get("calendar")
        rest = []
        for x in data["accounts"]:
            if x.get("id") in replaced:
                continue
            x = dict(x, areas=[a for a in x["areas"] if a not in acct["areas"]])
            if x["areas"]:
                rest.append(x if x.get("id") != "env" else dict(x, id=A.new_id()))
        data["accounts"] = [acct, *rest]
        new_cal = A.by_area(data).get("calendar")
        if (old_cal or {}).get("username") != (new_cal or {}).get("username"):
            data["calendars_visible"] = []
        A.save(settings.data_dir, data)
        log.warning("Owner connected a %s account on /setup; restarting", p)
        _restart()
        return done_page(who, acct)
