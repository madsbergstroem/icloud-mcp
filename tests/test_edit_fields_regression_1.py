"""Regressions found by /qa on the edit form of package 2d. Every name and id is made up.
Report: .gstack/qa-reports/run-20261117T102228Z/qa-report-localhost-2026-11-17.md"""
from pathlib import Path

from test_gateway_actions import call, item, market  # noqa: F401  (market is a fixture)
from test_dashboard import web  # noqa: F401  (web is a fixture)
from test_edit_fields import OFFER, editable, pending, put_fields  # noqa: F401  (editable is a fixture)


# Regression: ISSUE-001 — clearing an optional field saved a version whose changes list was empty
# Found by /qa on 2026-11-17
async def test_a_field_the_owner_empties_is_listed_as_a_change_of_that_version(web, editable):
    """protects=a version that drops an optional field (the offer) says so in its changes, so History and the card can show it;
    fails_when=versions_of compares only the rows the new version still has; no_claim=how History words it (the script)."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    got = await call(mcp, "approval_request", **OFFER)
    r = await put_fields(app, pending(mcp), {"offer": ""})
    assert r.status_code == 200 and r.json()["changed"], r.text
    it = await item(app, got["outbox_id"])
    assert "Offer (EUR)" not in dict(it["rows"])
    assert it["versions"][1]["changes"] == [{"label": "Offer (EUR)", "old": "35", "new": ""}]
    assert it["edited_rows"] == ["Offer (EUR)"]


# Regression: ISSUE-002 — History said "You changed the text" for an edit of the offer and the chat only
# Found by /qa on 2026-11-17
def test_history_names_the_fields_the_owner_changed():
    """protects=History names the fields of an owner's version, old and new value, instead of "You changed the text";
    fails_when=the owner branch of showHistory ignores row changes; no_claim=the rendering (checked in the preview by /qa)."""
    js = (Path(__file__).resolve().parents[1] / "src" / "icloud_mcp" / "static" / "app" / "today.js").read_text()
    said = js[js.index("showHistory()"):js.index("const items = vs.slice()")]
    assert "rows.length" in said and "You changed ${" in said, "the owner's field changes are not named in History"
    assert "removed" in said                                                     # an emptied field reads as removed, not as "→ "
