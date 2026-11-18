"""Regression: ISSUE-001 — an unclear calendar change told the owner to "look at the conversation", which only fits a chat.
Found by /qa on 2026-11-18
Report: .gstack/qa-reports/run-5a/qa-report-localhost-2026-11-18.md

Value: protects=the unclear notice takes its advice from the source's own note when there is one; fails_when=the chat-only line
is shown again next to a calendar note; why_new=no test reads the unclear notice; seam=none; no_claim=how the browser renders it
(checked by the QA run)."""
from pathlib import Path


def test_the_unclear_notice_leaves_the_advice_to_the_note_when_there_is_one():
    js = (Path(__file__).resolve().parents[1] / "src" / "icloud_mcp" / "static" / "app" / "today.js").read_text()
    said = js[js.index('if (r.state === "unclear")'):js.index('if (r.state === "failed") return')]
    assert "r.note ?" in said.split("Nothing is sent again.")[1].split("<div class=\"btns\">")[0], \
        "the chat-only advice is shown whatever the note says"
    assert "Look at the conversation first" in said                     # still there when the source gives no note
