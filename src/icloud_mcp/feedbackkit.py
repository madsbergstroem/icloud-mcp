"""Fork: "Send feedback" in the dashboard's Settings, from a FeedbackKit gateway (https://github.com/TruffleCraft/feedbackkit).

Off unless both FEEDBACKKIT_URL (the gateway's origin) and FEEDBACKKIT_PROJECT (the project's public key) are set. Then the app
page carries the gateway's documented snippet, `<script src="<gateway>/widget.js" data-project="<key>">`, without the widget's
floating button (`data-trigger="none"`: it would sit on the approve buttons), so Settings opens it, and its CSP lets the
browser reach that one origin for the script, its API calls and its font, as the gateway's QUICKSTART ("Host CSP") lists. The
server never contacts the gateway: the browser loads the widget and sends the report, so an egress-locked host needs no change.

The widget is not vendored: it is the gateway's own client and changes with the gateway's API, so it comes from the gateway.
What a report may carry (screenshots, console, where it goes) is the project's setting on the gateway, not this server's."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# an origin only: https with a host name, or plain http on this machine (a local gateway for a try-out)
_ORIGIN = re.compile(r"^(https://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::\d{1,5})?|http://(?:localhost|127\.0\.0\.1)(?::\d{1,5})?)$")
_KEY = re.compile(r"^fk_pub_[A-Za-z0-9]{6,64}$")


@dataclass(frozen=True)
class Widget:
    origin: str
    project: str

    def snippet(self) -> str:
        return f'<script src="{self.origin}/widget.js" data-project="{self.project}" data-trigger="none" defer></script>'

    def csp(self, policy: str) -> str:
        """The page's policy with the gateway added where the widget needs it: script, connect and font from the gateway,
        and blob: images for the screenshot preview. Its <style> is covered by the page's style-src 'unsafe-inline'."""
        directives: dict[str, list[str]] = {}
        for part in policy.split(";"):
            if words := part.split():
                directives[words[0]] = words[1:]
        for name, extra in (("script-src", self.origin), ("connect-src", self.origin), ("font-src", self.origin), ("img-src", "blob:")):
            values = [v for v in directives.get(name, []) if v != "'none'"]
            if extra not in values:
                values.append(extra)
            directives[name] = values
        return "; ".join(" ".join([k, *v]) for k, v in directives.items())


def from_settings(s) -> Widget | None:
    url, key = s.feedbackkit_url.rstrip("/").lower(), s.feedbackkit_project
    if not url and not key:
        return None
    if not _ORIGIN.match(url) or not _KEY.match(key):
        log.warning("Feedback button off: FEEDBACKKIT_URL must be a gateway origin (https://feedback.example.com) and "
                    "FEEDBACKKIT_PROJECT its public key (fk_pub_...)")
        return None
    return Widget(url, key)
