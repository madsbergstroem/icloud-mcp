"""Regression: ISSUE-002 — an unclear calendar change said "Not sure it reached Invitation.", naming the type as if it were a
place to look, next to the calendar's own advice.
Found by /qa on 2026-11-18
Report: .gstack/qa-reports/run-5b-20261118T132412Z/qa-report-localhost-2026-11-18.md

Value: protects=an unclear action with a note of its own leaves the advice to the note; fails_when=the line names the badge as a
place again when a note is given; why_new=test_unclear_notice_regression_1 reads the notice of the other kinds only; seam=none;
no_claim=how the browser renders it (checked by the QA run)."""
from pathlib import Path


def test_an_unclear_action_with_a_note_does_not_name_its_type_as_a_place():
    js = (Path(__file__).resolve().parents[1] / "src" / "icloud_mcp" / "static" / "app" / "today.js").read_text()
    line = next(x for x in js.splitlines() if 'r.state === "unclear" && it.kind === "external"' in x)
    with_note = line.split("r.note ?")[1].split(":")[0]
    assert "Not sure it went through." in with_note and "it.badge" not in with_note, "the type is named as the place to look"
    assert "Not sure it reached ${esc(it.badge)}." in line                # without a note of its own, as before
