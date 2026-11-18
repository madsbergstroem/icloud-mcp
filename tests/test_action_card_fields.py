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


# -- package 5b: a subject next to the title, a caption, and a text that lives inside another parameter ------------------------------
NOTE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["name", "doc"],
               "properties": {"name": {"type": "string"}, "doc": {"type": "string"}, "locked": {"type": "boolean"}}}


def _inner(p):
    return p["doc"].split("|", 1)[1]


def _with_inner(p, text):
    if "|" in text:
        raise ValueError("A bar cannot be part of the text.")
    return {**p, "doc": p["doc"].split("|", 1)[0] + "|" + text}


def doc_type(**kw):
    return A.ActionType(id="test.doc", title="Document", badge="Doc", approve_label="Save", params_schema=NOTE_SCHEMA,
                        text_field="doc", recipients=lambda p: ["tom@example.net"], subject=lambda p: p["name"],
                        text_caption="The note exactly as it will be saved.", text_of=_inner, with_text=_with_inner,
                        text_locked=lambda p: "This one is locked." if p.get("locked") else "", **kw)


def test_a_subject_and_a_caption_go_with_the_card_of_a_type_of_the_servers_own(tmp_path):
    """protects=package 5b, variant C: the title stays the type's own, the request's short label goes next to it as `subject`,
    and the type's caption as `text_caption`; fails_when=the subject becomes the title, or either is missing; no_claim=how the
    page draws them (the QA run)."""
    src = source(tmp_path, doc_type(), builtin=True)
    v = src.describe(src.queue_external("test.doc", {"name": "Autumn fair", "doc": "HEAD|Bring a cake"}, None))
    assert (v["title"], v["subject"], v["text_caption"], v["text"], v["label"]) == \
           ("Document", "Autumn fair", "The note exactly as it will be saved.", "Bring a cake", "tom@example.net")
    plain = source(tmp_path / "plain", chat_type())
    w = plain.describe(plain.queue_external("test.chat", {"chat": "Sam", "text": "Hi"}, None))
    assert "subject" not in w and "text_caption" not in w


def test_only_a_type_of_the_servers_own_may_have_a_subject():
    """protects=D4-2's reason for 5b's subject: a connector file or a plugin cannot put an agent's parameter next to the title;
    fails_when=the registration is accepted; no_claim=none beyond protects."""
    with pytest.raises(ValueError, match="subject"):
        A.ActionRegistry().register(doc_type(), Nothing(), ["*"])
    with pytest.raises(ValueError, match="text_of and with_text"):
        A.ActionType(id="test.half", title="Half", badge="Half", approve_label="Save", params_schema=NOTE_SCHEMA, text_field="doc",
                     text_of=_inner)


def test_a_text_inside_another_parameter_is_read_and_written_back_there(tmp_path):
    """protects=package 5b: the calendar's description lives inside its iCalendar object, so the text the owner edits is read
    from that parameter and written back into it, and the stored bytes are what is carried out; fails_when=the edit lands in a
    parameter of its own, the rest of the parameter is lost, an empty text is refused although the type allows it, or a text the
    type refuses is stored; no_claim=the calendar's own parsing (tests/test_text_edit.py)."""
    from icloud_mcp import textedit as T
    src = source(tmp_path, doc_type(), builtin=True)
    q = src.queue_external("test.doc", {"name": "Autumn fair", "doc": "HEAD|Bring a cake"}, None)
    assert src.text_block(q) == "" and src.text_of(q) == "Bring a cake"
    raw, _ = src.with_text(q, "Bring chairs")
    assert A.read(raw)[1] == {"name": "Autumn fair", "doc": "HEAD|Bring chairs"}
    assert A.read(src.with_text(q, "")[0])[1]["doc"] == "HEAD|"
    with pytest.raises(T.TextInvalid, match="bar"):
        src.with_text(q, "a|b")


def test_a_type_may_lock_the_text_of_one_request(tmp_path):
    """protects=package 5b: the text of one request stays as it is when its type says so (an object with more than one event),
    while other requests of the type can be edited; fails_when=the locked text can be changed or the reason is lost;
    no_claim=none beyond protects."""
    from icloud_mcp import textedit as T
    src = source(tmp_path, doc_type(), builtin=True)
    q = src.queue_external("test.doc", {"name": "Autumn fair", "doc": "HEAD|Bring a cake", "locked": True}, None)
    assert src.text_block(q) == "This one is locked." and src.describe(q)["text"] == "Bring a cake"
    with pytest.raises(T.TextLocked, match="locked"):
        src.with_text(q, "Bring chairs")


def test_the_store_takes_rows_whose_type_and_metadata_decide_their_new_form(tmp_path):
    """protects=package 5b for the store: a source whose request is its bytes plus metadata (a calendar change) moves with a
    conversion that sees the whole row, each row keeps the type its action_type column holds, the metadata leaves the row, and
    an ended row moves with its type; fails_when=the conversion misses the metadata, a row takes another's type, or a second run
    moves anything; no_claim=the calendar's own conversion (tests/test_calendar_release.py)."""
    old = Store(str(tmp_path), "calendar", 3600, 20)
    a = old.add(b"OBJ-A", ["jana@example.org"], {"op": "create", "summary": "Autumn fair"}, action_type="calendar.invite")
    b = old.add(b"OBJ-B", ["lead@example.org"], {"op": "reply", "summary": "Book club"}, action_type="calendar.reply")
    gone = old.add(b"OBJ-C", ["tom@example.net"], {"op": "delete"}, action_type="calendar.delete")
    old.claim(gone.id, "discarded")
    new = Store(str(tmp_path), "external", 3600, 20)

    def convert(raw, row):
        return json.dumps({"type": row["action_type"], "raw": raw.decode(), "summary": row["followup"]["summary"],
                           "to": row["recipients"]}, sort_keys=True).encode()
    moved = new.adopt("calendar", convert)
    assert sorted(x[0] for x in moved) == sorted([a.id, b.id])
    got = {q.id: (q.action_type, json.loads(q.raw), q.followup) for q in new.pending()}
    assert got[a.id] == ("calendar.invite", {"type": "calendar.invite", "raw": "OBJ-A", "summary": "Autumn fair", "to": ["jana@example.org"]}, None)
    assert got[b.id][0] == "calendar.reply" and got[b.id][1]["summary"] == "Book club"
    assert new.state_of(gone.id) == ("discarded", None) and old.pending() == [] and not new.has_source("calendar")
    assert new.adopt("calendar", convert) == []
