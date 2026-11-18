"""Regression: ISSUE-001 — a calendar change without a description still showed the caption "The description exactly as it will
be saved.", and a cancellation or a reply added "Edit text is not available" under it, although there is no description at all.
Found by /qa on 2026-11-18
Report: .gstack/qa-reports/run-5b-20261118T132412Z/qa-report-localhost-2026-11-18.md

Value: protects=an empty text of an action shows no caption that claims a text, and nothing at all where it cannot be edited
(variant C of package 5b); fails_when=the caption or the "not available" line is shown for an empty text again; why_new=the card
tests read the JSON, not how today.js lays it out; seam=none; no_claim=how the browser renders it (checked by the QA run)."""
from pathlib import Path


def test_an_empty_text_shows_no_caption_that_claims_one():
    js = (Path(__file__).resolve().parents[1] / "src" / "icloud_mcp" / "static" / "app" / "today.js").read_text()
    card = js[js.index("  card() {"):js.index("  showHistory() {")]
    capr = next(line for line in card.splitlines() if 'class="capr"' in line)
    why = next(line for line in card.splitlines() if "Edit text is not available" in line)
    # the caption row is left out for an empty action text unless the type names a caption and the text may be edited
    assert "ext && !own && !form && (!it.text_caption || !edit.allowed)" in capr, "a locked empty text still shows its caption"
    assert '!own && it.text_caption ? "No description."' in capr, "an empty editable text is captioned as if it had one"
    # the "not available" line never shows for an empty action text
    assert "edit.allowed || (ext && !own)" in why, "a cancellation without a description still says its text cannot be edited"
