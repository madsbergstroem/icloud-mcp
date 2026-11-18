"""Write the waiting requests from DATA_DIR/approvals.db back to their JSON queue files, for a rollback to a version without the
approval store (docs/SENDGATE.md, package 3, "Rollback"): external requests to external-outbox.json, and waiting iMessages to
imessage-outbox.json in the form 4a reads (packages 4a and 4b; that file is written only when an iMessage waits), and waiting
calendar changes to calendar-outbox.json in the form 5a reads (packages 5a and 5b; written only when one waits), and waiting
mail to outbox.json (package 6a; written only when a mail waits). It ships in the package, so it runs in the Docker image:

    docker run --rm -v <your data volume>:/data <this image> python -m icloud_mcp.gate.export_queue /data
    PYTHONPATH=src python -m icloud_mcp.gate.export_queue DATA_DIR          (from a checkout)

Stop the server first: one process holds DATA_DIR at a time. The exported requests end in the store (reason exported) in the same
step, so the JSON file is their only copy; an iMessage or a calendar change leaves the store, since the previous version reads it in
again under the same id. The command prints how many it wrote and what the rollback loses: requests that already
ended, among them unclear ones, and their keys, so a request_id used again after the rollback queues a new request.
Exit codes: 0 written, 1 refused (the server still runs, a request is being carried out, the file exists), 2 wrong arguments.
"""
import base64
import email
import sys
from email import policy
from email.message import EmailMessage
from typing import Any

from .store import Store


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        opened = {source: Store(argv[1], source, ttl=86400, max_items=10**6) for source in dict.fromkeys(src for src, *_ in PLAN)}
        plan = [(opened[src], name, kw) for src, name, kw in PLAN
                if name == "external-outbox.json" or opened[src].waiting_of(kw.get("only"), kw.get("skip", ()))]
        names = [name for _, name, _ in plan]
        if len(names) != len(set(names)):
            raise RuntimeError("iMessages or calendar changes wait in two forms. Start this version once, so it moves them to one, "
                               "then export again.")
        for store, name, _ in plan:                   # refuse before anything is written, so no file is half done
            store.check_export(name)
        done = [(name, store.export_waiting(name, **kw)) for store, name, kw in plan]
    except RuntimeError as e:
        print(f"Not exported: {e}", file=sys.stderr)
        return 1
    for name, got in done:
        print(f"Wrote {got['written']} waiting request(s) to {name}. A rollback loses {got['ended_lost']} ended request(s), "
              f"{got['unclear_lost']} of them unclear, and their keys.")
    return 0


# Since package 5b a calendar change is an action of one of five types; 5a keeps the object as the bytes and the rest as metadata
# with the operation. The type ids and the operations are the calendar's (cal.CALENDAR_TYPES); the gate does not import it.
CALENDAR = {"calendar.invite": "create", "calendar.update": "update", "calendar.delete": "delete", "calendar.cancel": "cancel",
            "calendar.reply": "reply"}


def calendar_object(type_id: str, p: dict[str, Any], blob: Any = None) -> tuple[bytes, dict[str, Any]]:
    """A calendar action back in the form 5a reads: the object as the bytes, and the metadata 5a's card and write use."""
    meta = {k: p.get(k) for k in ("calendar_url", "calendar", "uid", "href", "etag", "start", "end", "organizer")}
    return p["ics"].encode(), {**meta, "op": CALENDAR[type_id], "summary": p.get("summary") or "", "location": p.get("location") or ""}


# Since package 6b a mail is an action of type mail.send, mail.reply or mail.forward, and its parameters are the headers the owner
# may change (from, to, cc, bcc, subject), `message` (the rest of the MIME message: its other headers, the text, and each attachment
# as a part whose body is a blob, named by the header below), or `message_blob` when that rest is itself large, `files` and
# `followup`. mail_message puts them back together: the one place a mail's parameters become the bytes that go out, used by the
# export and by mail's executor (mail.py) alike. The gate does not import mail.
BLOB_HEADER = "X-Icloud-Mcp-Blob"
MAIL_HEADERS = (("From", "from"), ("To", "to"), ("Cc", "cc"), ("Bcc", "bcc"), ("Subject", "subject"))
MAIL = ("mail.send", "mail.reply", "mail.forward")


def mail_message(p: dict[str, Any], blob: Any) -> bytes:
    """The MIME message a mail action stands for, exactly as it is sent and filed: its five headers first, in the order the
    server writes them, then the rest of `message` with every attachment's bytes back in place. ValueError when a blob is
    missing or a header does not fit."""
    rest = blob(p["message_blob"]) if p.get("message_blob") else str(p.get("message") or "").encode("latin-1")
    if rest is None:
        raise ValueError("The body of this mail is missing from the approval store.")
    msg = email.message_from_bytes(rest, policy=policy.default)
    for part in [x for x in msg.walk() if x.get(BLOB_HEADER)]:
        data = blob(str(part[BLOB_HEADER]).strip())
        if data is None:
            raise ValueError(f"The attachment {part.get_filename() or ''} is missing from the approval store.".replace("  ", " "))
        del part[BLOB_HEADER]
        if part.get_content_type() == "message/rfc822":
            part.set_payload([email.message_from_bytes(data, policy=policy.default)])
        else:
            del part["Content-Transfer-Encoding"]
            part["Content-Transfer-Encoding"] = "base64"
            part.set_payload(base64.encodebytes(data).decode("ascii"))
    head = EmailMessage(policy=policy.SMTP)
    for name, field in MAIL_HEADERS:
        if p.get(field):
            head[name] = p[field]
    return head.as_bytes(policy=policy.SMTP)[:-2] + msg.as_bytes(policy=policy.SMTP)


def mail_object(type_id: str, p: dict[str, Any], blob: Any = None) -> tuple[bytes, dict[str, Any] | None]:
    """A mail action back in the form 6a reads: the message as the bytes, and its followup."""
    return mail_message(p, blob), p.get("followup")


# (source, file, which rows). Since package 4b an iMessage is an action of type imessage.send: it goes back to the file 4a reads,
# as the parameters it had, and stays out of the external file, where 4a would show it as an unknown action. The same for the
# calendar types since 5b.
IMESSAGE = "imessage.send"
PLAN = (("external", "external-outbox.json", {"skip": (IMESSAGE, *CALENDAR)}),
        ("external", "imessage-outbox.json", {"only": (IMESSAGE,), "as_params": True}),
        ("imessage", "imessage-outbox.json", {}),
        ("external", "calendar-outbox.json", {"only": tuple(CALENDAR), "as_object": calendar_object}),
        ("calendar", "calendar-outbox.json", {}),
        ("mail", "outbox.json", {}))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
