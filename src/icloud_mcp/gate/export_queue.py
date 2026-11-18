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
import sys
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


def calendar_object(type_id: str, p: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """A calendar action back in the form 5a reads: the object as the bytes, and the metadata 5a's card and write use."""
    meta = {k: p.get(k) for k in ("calendar_url", "calendar", "uid", "href", "etag", "start", "end", "organizer")}
    return p["ics"].encode(), {**meta, "op": CALENDAR[type_id], "summary": p.get("summary") or "", "location": p.get("location") or ""}


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
