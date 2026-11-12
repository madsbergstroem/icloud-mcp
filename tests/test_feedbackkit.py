"""Fork: the dashboard carries FeedbackKit's snippet only when FEEDBACKKIT_URL and FEEDBACKKIT_PROJECT are both set, and its CSP
then allows that one gateway origin, nothing else."""
from types import SimpleNamespace

import pytest

from icloud_mcp import feedbackkit
from test_dashboard import client, web  # noqa: F401  (web is a fixture)

GW, KEY = "https://feedback.example.com", "fk_pub_0123456789ab"


def directives(policy: str) -> dict[str, list[str]]:
    return {p.split()[0]: p.split()[1:] for p in policy.split(";") if p.strip()}


async def test_no_settings_no_button_and_the_page_policy_is_unchanged(web):
    app, *_ = web()
    async with client(app) as c:
        r = await c.get("/dashboard")
    assert "widget.js" not in r.text and "feedback" not in r.headers["content-security-policy"]
    assert directives(r.headers["content-security-policy"])["script-src"] == ["'self'"]


async def test_with_both_settings_the_page_loads_the_widget_from_the_gateway(web):
    app, *_ = web(FEEDBACKKIT_URL=GW + "/", FEEDBACKKIT_PROJECT=KEY)
    async with client(app) as c:
        r = await c.get("/dashboard")
    assert f'<script src="{GW}/widget.js" data-project="{KEY}" data-trigger="none" defer></script>' in r.text
    assert r.text.index("widget.js") < r.text.index("</head>")
    d = directives(r.headers["content-security-policy"])
    assert d["script-src"] == ["'self'", GW] and d["connect-src"] == ["'self'", GW]
    assert d["font-src"] == [GW] and d["img-src"] == ["'self'", "data:", "blob:"]
    assert d["default-src"] == ["'none'"] and d["frame-ancestors"] == ["'none'"] and d["frame-src"] == ["'self'"]


async def test_other_pages_keep_their_policy(web):
    app, *_ = web(FEEDBACKKIT_URL=GW, FEEDBACKKIT_PROJECT=KEY)
    async with client(app) as c:
        r = await c.get("/dashboard/app/app.css")
    assert GW not in r.headers["content-security-policy"]


@pytest.mark.parametrize("url,key", [
    (GW, ""), ("", KEY),                                         # only one of the two
    ("http://feedback.example.com", KEY),                        # plain http to another machine
    (GW + "/path", KEY), ("https://feedback.example.com\" onload=\"x", KEY), ("javascript:alert(1)", KEY),
    (GW, "fk_pub_x\" onload=\"x"), (GW, "not-a-key"),
])
def test_anything_but_an_origin_and_a_public_key_leaves_the_button_off(url, key):
    assert feedbackkit.from_settings(SimpleNamespace(feedbackkit_url=url, feedbackkit_project=key)) is None


def test_a_gateway_on_this_machine_is_allowed_for_a_try_out():
    w = feedbackkit.from_settings(SimpleNamespace(feedbackkit_url="http://localhost:8787", feedbackkit_project=KEY))
    assert w and w.origin == "http://localhost:8787"
