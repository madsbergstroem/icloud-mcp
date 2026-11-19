"""Mail that waits for the owner, as the server sets it up since package 6b (docs/SENDGATE.md): the mail service queues on the
external source as actions of the three mail types, and the owner's approval goes through the source's release. For tests that
build a MailService of their own."""
import dataclasses

from icloud_mcp.gate import actions as A
from icloud_mcp.gate.external import ExternalApprovals
from icloud_mcp.gate.store import Store
from icloud_mcp.mail import MAIL_TYPES, MailError, register_types


def held(mail):
    """The mail service with the external source the server gives it: its mail waits there as actions of the mail types."""
    reg = A.ActionRegistry()
    register_types(reg, mail)
    s = mail.s
    mail.external = ExternalApprovals(Store(s.data_dir, "external", s.outbox_ttl, s.outbox_max, keep_days=s.audit_keep_days,
                                            tombstones=tuple(MAIL_TYPES)), reg)
    return mail


def pending(mail):
    return mail.external.box.pending()


def approve(mail, item_id, sha256=None):
    """The owner's approval of a waiting mail, as the dashboard does it; sha256: the version the owner looked at. Returns
    {"status": sent | unclear, "note", "refused": how many the server refused}; raises Rechecked when it ended not_allowed,
    MailError when it waits again (in the words the owner reads)."""
    q = next((x for x in pending(mail) if x.id == item_id), None)
    if q is None:
        raise MailError("That message is no longer waiting (already released, discarded or expired).")
    try:
        got = mail.external.release(dataclasses.replace(q, sha256=sha256) if sha256 else q)
    except (A.Rechecked, A.Revised):
        raise
    except A.ExternalError as e:                             # it waits again
        raise MailError(str(e)) from None
    return {"status": {"unconfirmed": "unclear"}.get(got.delivery, got.delivery), "note": str(got), "refused": got.refused}
