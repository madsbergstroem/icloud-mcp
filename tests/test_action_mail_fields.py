"""Package 6b, the gate's half (docs/SENDGATE.md): what a type of the server's own needs to carry a mail. A request may name its
subject in a field the edit form offers first, carry files kept as blobs, wait for a missing file without being claimed, tag a row,
take what the card needs from a context read when it is shown, and have its recheck hear whom the owner typed in. A made-up type
("test.letter") and made-up people; each test says what it protects, the wrong result it rejects and what a green run does not
prove. The store's blobs are tested here too: kept while a version names them, gone in the same transaction once none does."""
import hashlib
import json
import time

import pytest

from icloud_mcp.gate import actions as A
from icloud_mcp.gate import store as S
from icloud_mcp.gate.external import ExternalApprovals
from icloud_mcp.gate.status import ApprovalLedger
from icloud_mcp.gate.store import Store

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["to", "subject", "body"],
          "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"},
                         "files": {"type": "array", "items": {"type": "object"}}}}


def _waits_for(p):
    pending = [f["name"] for f in p.get("files") or [] if f.get("pending")]
    return f"Not sent: it still waits for {', '.join(pending)}. Add the file first." if pending else ""


def letter_type(**kw):
    fields = dict(rows=(("Recipient", "to"),), text_field="body", editable_fields=("subject", "to"), recipient_fields=("to",),
                  recipients=lambda p: [x.strip() for x in p["to"].split(",") if x.strip()], subject_field="subject",
                  subject=lambda p: p["subject"], blobs=lambda p: [f["sha256"] for f in p.get("files") or [] if f.get("sha256")],
                  blocked=_waits_for)
    return A.ActionType(id="test.letter", title="Letter", badge="Letter", approve_label="Send", params_schema=SCHEMA, **{**fields, **kw})


class Executor:
    def __init__(self, result=None):
        self.calls, self.result = [], result or A.Result("sent", "Sent.")

    def execute(self, request):
        self.calls.append(request)
        return self.result


class LetterFiles:
    """Files of the made-up type: each entry of `files` is a file (sha256 of its blob) or a place waiting for one (pending)."""
    max_bytes = 1000

    def __init__(self):
        self.box = None

    def listed(self, p):
        return [{"index": i, "filename": f["name"], "size": f.get("size", 0), "content_type": "text/plain", "added": False, "asked": ""}
                for i, f in enumerate(p.get("files") or []) if not f.get("pending")]

    def missing(self, p):
        return [{"index": i, "filename": f["name"]} for i, f in enumerate(p.get("files") or []) if f.get("pending")]

    def content(self, p, index):
        f = (p.get("files") or [])[index]
        return (self.box.blob(f["sha256"]), "text/plain") if f.get("sha256") else None

    def put(self, p, data, *, filename, content_type, index=None, token=None):
        files = list(p.get("files") or [])
        hit = next((i for i, f in enumerate(files) if f.get("pending") and (i == index or f.get("token") == token)), None)
        if hit is None:
            raise LookupError("no place")
        files[hit] = {"name": files[hit]["name"], "size": len(data), "sha256": self.box.stage(data)}
        return {**p, "files": files}, {"index": hit, "filename": files[hit]["name"], "size": len(data), "asked": files[hit]["name"],
                                       "waiting": self.missing({"files": files})}

    def remove(self, p, index):
        files = list(p.get("files") or [])
        files[index] = {"name": files[index]["name"], "pending": True}
        return {**p, "files": files}, {"index": index, "asked": files[index]["name"], "waiting": self.missing({"files": files})}


def source(tmp_path, at=None, executor=None, **kw):
    reg = A.ActionRegistry()
    files = kw.pop("files", None)
    reg.register(at or letter_type(), executor or Executor(), ["*"], builtin=True, files=files, **kw)
    src = ExternalApprovals(Store(str(tmp_path), "external", 3600, 20), reg)
    if files is not None:
        files.box = src.box
    return src


def ask(src, **params):
    return src.queue_external("test.letter", {"to": "jana@example.org", "subject": "Invoice 2291", "body": "Hello", **params}, None)


# -- what a type of the server's own may have ----------------------------------------------------------------------------------
@pytest.mark.parametrize("field", ["files", "context", "allows", "status_kind", "recheck_owner_allowed"])
def test_only_a_type_of_the_servers_own_may_carry_files_a_context_or_its_own_list(field):
    """protects=the new fields read the server's own services or speak for the owner's lists, so a connector file or a plugin
    cannot use them (as with subject, D4-2); fails_when=a type that is not builtin registers with one; no_claim=none."""
    value = {"files": LetterFiles(), "context": lambda rs: {}, "allows": lambda p: True, "status_kind": "mail",
             "recheck_owner_allowed": True}[field]
    with pytest.raises(ValueError, match="only a type the server registers itself"):
        A.ActionRegistry().register(A.ActionType(id="test.plain", title="Plain", badge="Plain", approve_label="Go",
                                                 params_schema={"type": "object"}), Executor(), ["*"], **{field: value})


@pytest.mark.parametrize("field", ["subject_field", "blocked", "row_tags", "blobs"])
def test_only_a_type_of_the_servers_own_may_have_the_new_card_fields(field):
    """protects=as above, for the fields of ActionType; fails_when=a plugin type registers with one; no_claim=none."""
    value = {"subject_field": "subject", "blocked": lambda p: "", "row_tags": lambda p: {}, "blobs": lambda p: []}[field]
    at = A.ActionType(id="test.plain", title="Plain", badge="Plain", approve_label="Go", params_schema=SCHEMA, **{field: value})
    with pytest.raises(ValueError, match=f"only a type the server registers itself may have {field}"):
        A.ActionRegistry().register(at, Executor(), ["*"])


def test_the_subject_field_counts_as_shown_and_comes_first_in_the_edit_form(tmp_path):
    """protects=variant A: the subject is on the line under the title, not a row, yet the owner edits it first in the 2d form;
    fails_when=the type is refused for an editable field outside the rows, or the form puts the subject elsewhere or labels it
    otherwise; no_claim=how the form looks (the QA run)."""
    src = source(tmp_path)
    q = ask(src)
    form = src.fields_of(q)
    assert [(f["name"], f["label"]) for f in form] == [("subject", "Subject"), ("to", "Recipient"), ("body", "Text")]
    v = src.describe(q)
    assert (v["title"], v["subject"], v["rows"]) == ("Letter", "Invoice 2291", [("Recipient", "jana@example.org")])
    new, _, _, changed, _ = src.with_fields(q, {"subject": "Invoice 2291, corrected"})
    assert changed == ["Subject"] and json.loads(new)["params"]["subject"] == "Invoice 2291, corrected"


def test_a_locked_text_leaves_the_form_with_the_other_fields(tmp_path):
    """protects=a text the type cannot edit (a mail with an HTML part) still lets the owner change subject and recipients, and
    never the text; fails_when=the text field is offered or taken, or the whole form disappears; no_claim=none."""
    at = letter_type(text_of=lambda p: p["body"], with_text=lambda p, t: {**p, "body": t}, text_locked=lambda p: "It has an HTML part.")
    src = source(tmp_path, at)
    q = ask(src)
    assert [f["name"] for f in src.fields_of(q)] == ["subject", "to"]
    with pytest.raises(ValueError, match="body cannot be changed"):
        src.with_fields(q, {"body": "New text"})


def test_a_text_inside_another_parameter_is_edited_through_the_type_in_the_form(tmp_path):
    """protects=the form shows and writes the text the type reads (6b: the plain part of a mail), not the raw parameter;
    fails_when=the form shows the raw parameter or overwrites it with the text; no_claim=none."""
    at = letter_type(text_of=lambda p: p["body"].removeprefix("RAW:"), with_text=lambda p, t: {**p, "body": "RAW:" + t})
    src = source(tmp_path, at)
    q = ask(src, body="RAW:Hello")
    assert next(f for f in src.fields_of(q) if f["name"] == "body")["value"] == "Hello"
    raw, _, _, changed, _ = src.with_fields(q, {"body": "Hello again", "subject": "Invoice"})
    assert changed == ["Subject", "Text"] and json.loads(raw)["params"]["body"] == "RAW:Hello again"


# -- a request that cannot go out yet, a context, a tag ------------------------------------------------------------------------
def test_a_blocked_request_is_never_claimed_and_its_card_says_why(tmp_path):
    """protects=a block per request (a missing file): approving refuses before anything is claimed, so the request is not counted
    as carried out and the executor never runs; fails_when=it is claimed (attempts goes up), carried out, or the card lacks the
    reason; no_claim=the dashboard's own refusal (test_mail_release.py)."""
    ex = Executor()
    src = source(tmp_path, executor=ex)
    q = ask(src, files=[{"name": "Quote.pdf", "pending": True}])
    assert src.describe(q)["blocked"] == "Not sent: it still waits for Quote.pdf. Add the file first."
    with pytest.raises(A.ExternalError, match="still waits for Quote.pdf"):
        src.release(q)
    assert src.box.state_of(q.id) == ("waiting", None) and ex.calls == []
    assert src.box._db.execute("SELECT attempts FROM request WHERE id = ?", (q.id,)).fetchone()[0] == 0


def test_a_block_that_cannot_be_worked_out_blocks(tmp_path):
    """protects=approving never gets easier because the type's function failed; fails_when=it is carried out; no_claim=none."""
    ex = Executor()
    src = source(tmp_path, letter_type(blocked=lambda p: 1 / 0), ex)
    q = ask(src)
    with pytest.raises(A.ExternalError):
        src.release(q)
    assert ex.calls == []


def test_a_context_adds_cautions_a_second_confirmation_and_what_the_request_answers(tmp_path):
    """protects=what mail read from the mailbox when the card is shown (the mail a reply answers and its steering warnings, D6-7)
    reaches the card of an action; fails_when=the warning, the second confirmation or the original is lost, or the context is
    asked once per request instead of once per type; no_claim=what mail reads (test_mail_release.py)."""
    seen = []

    def context(requests):
        seen.append([r.id for r in requests])
        return {r.id: {"warnings": ["The mail it answers looked like an attempt to steer the agent."], "confirm": "It answers a suspicious mail.",
                       "original": {"label": "In reply to", "subject": "Invoice 2291", "from": "accounts@example.org"}} for r in requests}
    src = source(tmp_path, context=context)
    a, b = ask(src), ask(src, to="tom@example.net")
    ctx = src.contexts([a, b])
    assert seen == [[a.id, b.id]]
    v = src.describe(a, ctx[a.id])
    assert v["warnings"] == ["The mail it answers looked like an attempt to steer the agent."] and v["needs_override"] is True
    assert v["original"] == {"label": "In reply to", "subject": "Invoice 2291", "from": "accounts@example.org"}
    assert "original" not in src.describe(a) and src.describe(a)["needs_override"] is False


def test_a_row_may_carry_a_tag(tmp_path):
    """protects=the account tag on a mail's From row; fails_when=the tag is lost or one that raises breaks the card;
    no_claim=how the tag looks."""
    src = source(tmp_path, letter_type(row_tags=lambda p: {"Recipient": "iCloud"}))
    assert src.describe(ask(src))["row_tags"] == {"Recipient": "iCloud"}
    broken = source(tmp_path / "b", letter_type(row_tags=lambda p: 1 / 0))
    assert "row_tags" not in broken.describe(ask(broken))


# -- the owner's own list, through the recheck -----------------------------------------------------------------------------------
def test_the_recheck_hears_whom_the_owner_typed_in_and_ends_it_in_its_own_words(tmp_path):
    """protects=the review's override requirement: with allowed ["*"] the gate records no list of its own, so the type's recheck is
    told whom the owner typed in, and a refusal ends the request not_allowed in the type's words, never as a wait; fails_when=the
    recheck does not get the owner's people, the request waits again, or the executor runs; no_claim=mail's own rules
    (test_mail_release.py)."""
    seen, ex = [], Executor()

    def recheck(p, own):
        seen.append(list(own))
        people = [x.strip() for x in p["to"].split(",")]
        if len(people) > 2:
            raise A.Rechecked("not_allowed", f"Not sent: Too many recipients ({len(people)}); limit is 2.")
        if any(x not in ("jana@example.org", *own) for x in people):
            raise A.Rechecked("not_allowed", "Not sent: Recipient(s) not permitted.")
        return None
    src = source(tmp_path, executor=ex, recheck=recheck, recheck_owner_allowed=True, allows=lambda p: p == "jana@example.org")
    q = ask(src)
    raw, _, _, _, own = src.with_fields(q, {"to": "jana@example.org, tom@example.net"})
    assert own == ["tom@example.net"]
    new = src.box.replace(q.id, q.sha256, raw, recipients=["jana@example.org", "tom@example.net"])
    assert src.fields_of(new)[1]["owner_allowed"] is True
    with pytest.raises(ValueError, match="Too many recipients"):          # MAX is never waived, and the save says so
        src.with_fields(new, {"to": "jana@example.org, tom@example.net, sam@example.net"})
    assert src.release(new) == "Sent." and seen[-1] == ["tom@example.net"] and len(ex.calls) == 1
    other = ask(src, to="sam@example.net")
    with pytest.raises(A.Rechecked) as e:
        src.release(other)
    assert e.value.reason == "not_allowed" and str(e.value) == "Not sent: Recipient(s) not permitted."
    assert src.box.state_of(other.id) == ("discarded", "not_allowed") and len(ex.calls) == 1


def test_a_count_of_refused_recipients_reaches_the_owner_s_line(tmp_path):
    """protects=M4 on the external source: the executor's count of recipients the service refused goes to the log line;
    fails_when=it is lost; no_claim=who they were (the note says it)."""
    src = source(tmp_path, executor=Executor(A.Result("sent", "Sent to jana@example.org. The mail server refused 1: tom@example.net.", 1)))
    got = src.release(ask(src))
    assert (got.delivery, got.refused) == ("sent", 1)


def test_approval_status_names_the_kind_a_type_asks_for(tmp_path):
    """protects=D6-8: a mail on the external source still answers kind mail, with its type, waiting and after it ended;
    fails_when=it answers external, or another type's kind changes; no_claim=the log line itself (test_mail_answers.py)."""
    from icloud_mcp.audit import AuditLog
    src = source(tmp_path)
    q = ask(src)
    audit = AuditLog(str(tmp_path))
    ledger = ApprovalLedger({"external": src.box}, audit, kinds=lambda t: "mail" if t.startswith("test.letter") else None)
    assert (ledger.status(q.id)["kind"], ledger.status(q.id)["action_type"]) == ("mail", "test.letter")
    src.box.claim(q.id, "discarded")
    audit.record("approval.discarded", actor="owner", kind="external", outbox_id=q.id, action_type="test.letter")
    assert ledger.status(q.id)["kind"] == "mail"
    plain = ApprovalLedger({"external": src.box}, audit)
    assert plain.status(q.id)["kind"] == "external"


# -- files ------------------------------------------------------------------------------------------------------------------------
def test_a_file_put_into_its_place_makes_a_new_version_and_its_blob_is_kept(tmp_path):
    """protects=the upload and the owner's file on the external source: the file lands in the place the token or the index names,
    the request waits with a new hash, and the bytes are in the store; fails_when=another place is filled, the request ends, or
    the bytes are lost; no_claim=mail's own places (test_mail_release.py)."""
    files = LetterFiles()
    src = source(tmp_path, files=files)
    q = ask(src, files=[{"name": "Quote.pdf", "pending": True, "token": "t1"}])
    with pytest.raises(LookupError):
        src.put_file(b"x", filename="Quote.pdf", content_type="text/plain", token="t2")
    new, info = src.put_file(b"%PDF made up", filename="Quote.pdf", content_type="application/pdf", token="t1")
    assert info["waiting"] == [] and new.sha256 != q.sha256 and src.box.state_of(q.id) == ("waiting", None)
    assert src.file_content(new, 0) == (b"%PDF made up", "text/plain") and "blocked" not in src.describe(new)
    back, _ = src.remove_file(q.id, 0)
    assert src.files_of(back)["missing"] == [{"index": 0, "filename": "Quote.pdf"}]


def _blobs(box):
    return {r[0] for r in box._db.execute("SELECT sha256 FROM blob")}


def test_a_blob_stays_while_a_version_names_it_and_goes_in_the_same_transaction_once_none_does(tmp_path):
    """protects=the review's D6-5 requirement: two mails sharing one file keep it until both ended, and a file replaced between
    versions stays while an older version names it; fails_when=a blob goes while named (the send would miss it) or stays once
    nothing names it; no_claim=the size of the database file (SQLite reuses the pages)."""
    files = LetterFiles()
    src = source(tmp_path, files=files)
    box = src.box
    one = box.stage(b"shared file")
    a = ask(src, files=[{"name": "a.txt", "sha256": one}])
    b = ask(src, to="tom@example.net", files=[{"name": "a.txt", "sha256": one}])
    box.claim(a.id, "discarded")
    assert one in _blobs(box)                                       # b still names it
    two = box.stage(b"the replacement")
    raw = A.canonical("test.letter", {**json.loads(b.raw)["params"], "files": [{"name": "a.txt", "sha256": two}]})
    newer = box.replace(b.id, b.sha256, raw)
    assert {one, two} <= _blobs(box)                                # version 1 still names the first
    three = box.stage(b"and once more")
    box.replace(b.id, newer.sha256, A.canonical("test.letter", {**json.loads(b.raw)["params"], "files": [{"name": "a.txt", "sha256": three}]}))
    assert two not in _blobs(box) and {one, three} <= _blobs(box)    # the owner's edits in a row are one version
    box.claim(b.id, "withdrawn")
    assert _blobs(box) == set()


def test_a_version_that_names_a_blob_the_store_does_not_hold_is_refused(tmp_path):
    """protects=nothing is queued that could not be sent as approved; fails_when=it is queued; no_claim=none."""
    src = source(tmp_path)
    with pytest.raises(A.ExternalError) as e:
        ask(src, files=[{"name": "a.txt", "sha256": hashlib.sha256(b"never staged").hexdigest()}])
    assert e.value.code == "store_unavailable" and src.box.pending() == []


def test_a_staged_blob_nothing_named_goes_after_an_hour(tmp_path):
    """protects=a blob staged for a request that was then refused does not stay for ever; fails_when=it stays, or one goes
    within the hour; no_claim=none."""
    box = Store(str(tmp_path), "external", 3600, 20)
    sha = box.stage(b"left behind")
    with box._lock, box._tx():
        box._gc()
    assert sha in _blobs(box)
    box._db.execute("UPDATE blob SET staged_at = ?", (time.time() - 3700,))
    with box._lock, box._tx():
        box._gc()
    assert sha not in _blobs(box)


def test_tombstones_may_be_kept_for_some_types_only(tmp_path):
    """protects=mail's keys outlive their rows on the external source (D6-3) without binding every key an agent ever used;
    fails_when=a key of another type is kept, or mail's is not; no_claim=none."""
    box = Store(str(tmp_path), "external", 3600, 20, keep_days=1, tombstones=("mail.send",))
    m = box.add(b"m", [], key="draft:Drafts:7:4", action_type="mail.send")
    o = box.add(b"o", [], key="req-1", action_type="test.letter")
    for q in (m, o):
        box.claim(q.id, "discarded")
    box._db.execute("UPDATE request SET decided_at = ?", (time.time() - 3 * 86400,))
    box._purged_at = 0
    box.pending()
    assert [r[0] for r in box._db.execute("SELECT key FROM key_tombstone")] == ["draft:Drafts:7:4"]
    with pytest.raises(S.AlreadyDecided):
        box.add(b"m2", [], key="draft:Drafts:7:4", action_type="mail.send")
    assert box.add(b"o2", [], key="req-1", action_type="test.letter").key == "req-1"
