"""Package 4b, the gate's half (docs/SENDGATE.md): an action type may say how its card looks (tags, a chat bubble, a second
confirmation, its own note), and the store takes the rows of another source in once. A made-up type and made-up people; each test
says what it protects, the wrong result it rejects and what a green run does not prove."""
import hashlib
import json

import pytest

from icloud_mcp.gate import actions as A
from icloud_mcp.gate.external import ExternalApprovals
from icloud_mcp.gate.store import Store

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["chat", "text"],
          "properties": {"chat": {"type": "string"}, "group": {"type": "boolean"}, "text": {"type": "string", "minLength": 1}}}


def chat_type(**kw):
    return A.ActionType(id="test.chat", title="Chat message", badge="Chat", approve_label="Send", params_schema=SCHEMA,
                        rows=(("Chat", "chat"),), text_field="text", recipients=lambda p: [p["chat"]], **kw)


class Nothing:
    def execute(self, request):
        return A.Result("sent", "Done.")


def source(tmp_path, at, **kw):
    reg = A.ActionRegistry()
    reg.register(at, Nothing(), ["*"], **kw)
    return ExternalApprovals(Store(str(tmp_path), "external", 3600, 20), reg)


def test_a_card_says_what_its_type_asks_for(tmp_path):
    """protects=the card of a chat message: its Group tag, the bubble, the second confirmation with the type's reason and the
    type's own note; fails_when=any of them is missing or the general note for actions shows; no_claim=how the page draws them
    (the QA run)."""
    at = chat_type(tags=lambda p: ["Group"] if p.get("group") else [], text_style="bubble",
                   confirm_twice=lambda p: f"Everyone in {p['chat']} receives it." if p.get("group") else None,
                   note="Sent from your Mac once you approve.")
    src = source(tmp_path, at, builtin=True)
    v = src.describe(src.queue_external("test.chat", {"chat": "Weekend hike", "group": True, "text": "Hi"}, None))
    assert (v["title"], v["tags"], v["text_style"], v["needs_override"], v["confirm"], v["log_to"]) == \
           ("Chat message", ["Group"], "bubble", True, "Everyone in Weekend hike receives it.", True)
    assert v["notes"] == ["Sent from your Mac once you approve."]
    one = src.describe(src.queue_external("test.chat", {"chat": "Anna", "text": "Hi"}, None))
    assert (one["tags"], one["needs_override"], "confirm" in one) == ([], False, False)


def test_a_type_without_the_fields_looks_as_before(tmp_path):
    """protects=gateway and plugin types: no tags, plain text, no second confirmation, the general note, nothing named in the
    log; fails_when=a default changes how they look; no_claim=none beyond protects."""
    src = source(tmp_path, chat_type())
    v = src.describe(src.queue_external("test.chat", {"chat": "Anna", "text": "Hi"}, None))
    assert (v["tags"], v["text_style"], v["needs_override"], v["log_to"]) == ([], "plain", False, False)
    assert v["notes"] == ["Approving carries this out. If it is not certain that it went through, it is never repeated."]


def test_a_second_confirmation_that_cannot_be_worked_out_still_asks(tmp_path):
    """protects=approving never gets easier because the type's function failed; fails_when=needs_override is False;
    no_claim=none beyond protects."""
    src = source(tmp_path, chat_type(confirm_twice=lambda p: 1 / 0))
    v = src.describe(src.queue_external("test.chat", {"chat": "Anna", "text": "Hi"}, None))
    assert v["needs_override"] is True and v["confirm"]


def test_an_unknown_text_style_stops_the_start():
    """protects=a typo in a type shows at start-up, not as a card that falls back silently; fails_when=the type is created;
    no_claim=none beyond protects."""
    with pytest.raises(ValueError, match="text_style"):
        chat_type(text_style="balloon")


def test_the_store_takes_another_sources_rows_once_with_ids_states_and_versions(tmp_path):
    """protects=package 4b criterion 3 for the store: every row of the old source moves with its id, state and versions, a
    waiting one gets the hash of its new bytes, an ended one keeps what says what it was, and a second run moves nothing;
    fails_when=a row is lost, copied, keeps the old hash, or stays under the old source; no_claim=what the new bytes are (the
    iMessage tests)."""
    old = Store(str(tmp_path), "imessage", 3600, 20)
    waiting = old.add(b'{"text": "Hi"}', ["anna@example.org"])
    edited = old.replace(waiting.id, waiting.sha256, b'{"text": "Hi there"}')
    gone = old.add(b'{"text": "Bye"}', ["tom@example.net"])
    old.claim(gone.id, "discarded")
    new = Store(str(tmp_path), "external", 3600, 20)
    convert = lambda raw: json.dumps({"action_type": "test.chat", "params": json.loads(raw)}, sort_keys=True).encode()  # noqa: E731
    moved = new.adopt("imessage", convert, "test.chat")
    want = convert(b'{"text": "Hi there"}')
    assert moved == [(waiting.id, edited.sha256, hashlib.sha256(want).hexdigest())]
    assert [(q.id, q.raw, q.action_type) for q in new.pending()] == [(waiting.id, want, "test.chat")]
    assert [v["raw"] for v in new.versions(waiting.id)] == [convert(b'{"text": "Hi"}'), want]
    assert new.state_of(gone.id) == ("discarded", None) and old.pending() == [] and old.state_of(gone.id) is None
    assert new.adopt("imessage", convert, "test.chat") == []


def test_a_recheck_that_cannot_look_says_why_and_the_request_waits(tmp_path):
    """protects=package 4a row 1 on the external source: a read that fails after the approval leaves the request waiting, and
    the owner reads the type's reason; fails_when=it ends, or the reason is replaced by a general line; no_claim=none beyond
    protects."""
    def recheck(p):
        raise A.ExternalError("The Mac has not connected since 09:00.")
    reg = A.ActionRegistry()
    reg.register(chat_type(), Nothing(), ["*"], recheck=recheck)
    src = ExternalApprovals(Store(str(tmp_path), "external", 3600, 20), reg)
    q = src.queue_external("test.chat", {"chat": "Anna", "text": "Hi"}, None)
    with pytest.raises(A.ExternalError, match="^The Mac has not connected since 09:00"):
        src.release(q)
    assert src.box.state_of(q.id) == ("waiting", None)
