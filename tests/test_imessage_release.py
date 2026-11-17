"""Package 4a (docs/SENDGATE.md): a queued iMessage lives in the approval store, and every way an approval can end is a state,
so nothing is lost while the card says it waits and nothing is sent twice. One test per row of the table in 4a. Made-up people,
chats and answers of the Mac; each test says what it protects, the wrong result it rejects and what a green run does not prove."""
import copy
import dataclasses
import json

import pytest

from icloud_mcp.bridge import BridgeError
from icloud_mcp.gate.actions import Rechecked, Revised
from icloud_mcp.imessage import IMessageError

import test_imessage_send as base
from test_imessage_send import Bridge, s, svc  # noqa: F401  (s is a fixture)



def sends(b):
    return [a for op, a in b.calls if op == "imessage_send"]


def group_ready(s, tmp_path, **kw):
    m, b, box = svc(dataclasses.replace(s, imessage_send_allowlist=("anna@example.org", "+31600000002", "+31600000003")), tmp_path, **kw)
    q = m.send("Hi all", chat_id="chat900", outbox=box)["outbox_id"]
    return m, b, box, q


@pytest.fixture(autouse=True)
def fresh_chats(monkeypatch):
    monkeypatch.setattr(base, "CHATS", copy.deepcopy(base.CHATS))       # a test that changes a group changes only its own copy


def test_a_read_that_fails_after_the_approval_leaves_it_waiting(s, tmp_path):
    """protects=row 1: only a read happened, so the request waits again and the owner sees why; fails_when=it is lost, ends, or a
    send is tried; no_claim=the dashboard's wording (test_messages_send)."""
    m, b, box, q = group_ready(s, tmp_path)
    m.bridge = Bridge(fail=None)
    m.bridge.call = lambda op, a=None: (_ for _ in ()).throw(BridgeError("The Mac has not connected since 09:00"))
    with pytest.raises(IMessageError, match="still waiting"):
        m.release(box, q)
    assert [x.id for x in box.pending()] == [q] and box.state_of(q)[0] == "waiting"


def test_members_who_changed_but_are_allowed_make_a_new_version_and_nothing_is_sent(s, tmp_path):
    """protects=row 4: the owner approved the people they saw; someone who joined since is shown first, as a version by the
    service; fails_when=it is sent to the new group, or the request ends; no_claim=the History sheet's wording (2c tests)."""
    m, b, box, q = group_ready(s, tmp_path)
    base.CHATS["chats"][1]["participants"].append("+31600000003")
    sha = box.pending()[0].sha256
    with pytest.raises(Revised) as got:
        m.release(box, q, sha)
    assert got.value.n == 2 and not sends(b)
    now = box.pending()[0]
    assert now.sha256 == got.value.sha256 != sha and box.versions(q)[-1]["author"] == "service"
    assert [p["handle"] for p in json.loads(now.raw)["to"]["participants"]] == ["+31600000002", "+31600000003", "anna@example.org"]
    assert m.release(box, q, now.sha256)["status"] == "sent" and len(sends(b)) == 1


def test_a_refusal_of_the_mac_that_says_nothing_was_sent_leaves_it_waiting(s, tmp_path):
    """protects=row 5: the Mac refused before Messages had it, so a retry cannot deliver it twice; fails_when=it ends or reads
    unclear; no_claim=which Mac errors say so (mac-helper/ops/imessage.py writes them)."""
    m, b, box = svc(s, tmp_path, fail=BridgeError("Messages does not know that conversation or handle (no iMessage). Nothing was sent."))
    q = m.send("Hi", handle="anna@example.org", outbox=box)["outbox_id"]
    with pytest.raises(IMessageError, match="still waiting"):
        m.release(box, q)
    assert box.state_of(q)[0] == "waiting"


@pytest.mark.parametrize("answer,state", [({"status": "sent", "message_id": "m1"}, "sent"),
                                          ({"status": "failed", "message_id": "m1"}, "unclear"),
                                          ({"status": "unconfirmed"}, "unclear")])
def test_the_macs_answer_ends_it_once(s, tmp_path, answer, state):
    """protects=rows 7 and 8: sent ends sent; a send Messages did not confirm or could not deliver may have gone out, so it ends
    unclear and is never sent again; fails_when=another state, or a second approval sends again; no_claim=delivery on the phone."""
    m, b, box = svc(s, tmp_path, send_answer=answer)
    q = m.send("Hi", handle="anna@example.org", outbox=box)["outbox_id"]
    assert m.release(box, q)["status"] == answer["status"]
    assert box.state_of(q)[0] == state and not box.pending()
    with pytest.raises(IMessageError, match="no longer waiting"):
        m.release(box, q)
    assert len(sends(b)) == 1


def test_a_version_the_owner_did_not_look_at_is_not_sent(s, tmp_path):
    """protects=the click sends what the owner saw: an edit in between makes another sha; fails_when=the newer text goes out;
    no_claim=none beyond protects."""
    m, b, box = svc(s, tmp_path)
    q = m.send("Hi", handle="anna@example.org", outbox=box)["outbox_id"]
    with pytest.raises(IMessageError, match="changed just now"):
        m.release(box, q, "0" * 64)
    assert box.state_of(q)[0] == "waiting" and not sends(b)


def test_a_chat_the_owner_hid_after_queuing_is_not_sent(s, tmp_path):
    """protects=row 3 for the owner's other list: a conversation hidden since it was queued ends not_allowed without asking the
    Mac; fails_when=it is sent or waits on; no_claim=none beyond protects."""
    m, b, box = svc(s, tmp_path)
    q = m.send("Hi", handle="anna@example.org", outbox=box)["outbox_id"]
    m.s = dataclasses.replace(s, imessage_hidden_chats=("anna@example.org",))
    with pytest.raises(Rechecked, match="hid") as got:
        m.release(box, q)
    assert got.value.reason == "not_allowed" and box.state_of(q) == ("discarded", "not_allowed") and not sends(b)


def test_the_server_reads_the_old_queue_file_in_and_runs_imessages_from_the_store(s, tmp_path):
    """protects=package 4a criterion 3 at start-up: an upgrade keeps every waiting iMessage with its id and hash, and the server
    queues new ones in approvals.db; fails_when=the file is ignored, read twice, or the server still writes the JSON file;
    no_claim=the Docker volume (same DATA_DIR rules)."""
    from icloud_mcp.outbox import Outbox
    from icloud_mcp.server import create_server
    old = Outbox(str(tmp_path), 3600, 20, filename="imessage-outbox.json")
    q = old.add(b'{"chat_id": null, "handle": "anna@example.org", "text": "Hi", "to": {"participants": []}}', ["anna@example.org"])
    mcp, _ = create_server(s)
    box = mcp._icloud_outboxes["imessage"]
    assert box.path.name == "approvals.db" and [(x.id, x.sha256) for x in box.pending()] == [(q.id, q.sha256)]
    assert (tmp_path / "imessage-outbox.json.migrated").exists() and not (tmp_path / "imessage-outbox.json").exists()
