"""/setup: the owner connects their Apple Account (address, app-specific password, sender) in the browser.

Only served when owner sign-in through Cloudflare Access is configured (access_sso.py), and only to a request that
carries a verified owner identity: there is no password fallback, because the page exists so that the owner does not
have to hand the app-specific password to anyone else (an admin, a chat, an agent). A deployment for a new person can
start with a placeholder account (an address ending in `.invalid`); the person then enters everything here. The account
is proven against iCloud Mail before it is stored in DATA_DIR/overrides.json, then the server exits so the container
restart policy brings it back with the new values. The password is never logged or echoed back.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
import secrets
import threading
import time
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from . import access_sso
from .admin import check_and_store_app_password
from .auth import _HEADERS
from .config import Settings

log = logging.getLogger(__name__)

TOKEN_TTL = 600  # seconds a form stays valid

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark"><title>iCloud connector setup</title>
<style>
:root{{color-scheme:light dark;--bg:#f5f5f7;--card:#fff;--text:#1d1d1f;--muted:#666;--line:#bbb;--accent:#0071e3;--err:#c00;--ok:#1a7f37;--shadow:#0002}}
@media (prefers-color-scheme:dark){{:root{{--bg:#000;--card:#1c1c1e;--text:#f5f5f7;--muted:#98989d;--line:#48484a;--accent:#0a84ff;--err:#ff6961;--ok:#3fb950;--shadow:#0000}}}}
body{{font:16px/1.5 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);margin:0;display:grid;place-items:center;min-height:100vh}}
main{{background:var(--card);padding:2rem;border-radius:14px;max-width:26rem;width:calc(100% - 2rem);box-shadow:0 2px 20px var(--shadow)}}
h1{{font-size:1.25rem;margin:0 0 .5rem}} p{{margin:.5rem 0}} .err{{color:var(--err)}} .ok{{color:var(--ok)}} .muted{{color:var(--muted);font-size:.9rem}}
input[type=password],input[type=email],input[type=text]{{width:100%;box-sizing:border-box;padding:.7rem;font-size:1rem;border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:8px;margin:.5rem 0 1rem}}
button{{padding:.7rem 1.2rem;font-size:1rem;border-radius:8px;border:0;cursor:pointer;background:var(--accent);color:#fff}}
</style></head><body><main><h1>iCloud connector setup</h1>{body}</main></body></html>"""


def _html(body: str, status: int = 200) -> Response:
    return HTMLResponse(_PAGE.format(body=body), status_code=status, headers=_HEADERS)


def register_setup_routes(mcp: Any, settings: Settings, *, exit_fn: Callable[[], None] | None = None) -> None:
    access = access_sso.from_settings(settings)
    if access is None:
        return  # without a verified owner identity there is no safe way to offer this page
    key = secrets.token_bytes(32)  # per-process: a form issued before a restart simply asks again
    exit_fn = exit_fn or (lambda: os._exit(0))  # the container restart policy starts the server again

    def _tok(who: str, exp: int) -> str:
        return hmac.new(key, f"setup|{who}|{exp}".encode(), hashlib.sha256).hexdigest()

    def _form(who: str, error: str = "", status: int = 200) -> Response:
        exp = int(time.time()) + TOKEN_TTL
        pending = settings.username.lower().endswith(".invalid")
        current = "" if pending else settings.username
        name = "" if pending else settings.display_name
        sender = "" if pending or settings.email_address == settings.username else settings.email_address
        return _html(f"""<p class="muted">Signed in as <b>{html.escape(who)}</b> through Cloudflare Access.</p>
<p>Connect your Apple Account. Create an app-specific password at account.apple.com (Sign-In and Security, App-Specific
Passwords). Everything is checked with iCloud Mail before it is saved, and only this server keeps it.</p>{f'<p class="err">{html.escape(error)}</p>' if error else ''}
<form method="post" action="/setup"><input type="hidden" name="exp" value="{exp}"><input type="hidden" name="tok" value="{_tok(who, exp)}">
<label for="ac">Apple Account (email address)</label>
<input id="ac" type="email" name="username" value="{html.escape(current)}" autocomplete="username" required>
<label for="pw">App-specific password</label>
<input id="pw" type="password" name="password" autocomplete="off" placeholder="abcd-efgh-ijkl-mnop" required>
<label for="em">Send mail as (optional, an iCloud address or alias)</label>
<input id="em" type="email" name="email_address" value="{html.escape(sender)}" autocomplete="email">
<label for="dn">Your name on outgoing mail (optional)</label>
<input id="dn" type="text" name="display_name" value="{html.escape(name)}" autocomplete="name" maxlength="200">
<button type="submit">Check and save</button></form>""", status)

    @mcp.custom_route("/setup", methods=["GET"])
    async def setup_get(request: Request) -> Response:
        who = await access.owner_email(request.headers)
        if not who:
            return _html('<p class="err">This page is only available after signing in through Cloudflare Access.</p>', 403)
        return _form(who)

    @mcp.custom_route("/setup", methods=["POST"])
    async def setup_post(request: Request) -> Response:
        who = await access.owner_email(request.headers)
        if not who:
            return _html('<p class="err">This page is only available after signing in through Cloudflare Access.</p>', 403)
        form = await request.form()
        try:
            exp = int(str(form.get("exp", "0")))
        except ValueError:
            exp = 0
        if exp < time.time() or not hmac.compare_digest(str(form.get("tok", "")).encode(), _tok(who, exp).encode()):
            log.warning("Rejected a /setup submission with an expired or invalid token")
            return _form(who, "This form has expired. Enter the password again.", 403)
        account = {k: str(form.get(k, "")) for k in ("username", "email_address", "display_name")}
        if problem := await check_and_store_app_password(settings, str(form.get("password", "")), "the /setup page", account=account):
            return _form(who, problem, 400)
        log.warning("Owner connected the Apple Account on /setup; restarting")
        threading.Timer(1.0, exit_fn).start()  # after the response has gone out
        return _html('<p class="ok">Saved. The connector restarts with the new password and is back in about 30 seconds.</p>'
                     '<p class="muted">You can close this page.</p>')
