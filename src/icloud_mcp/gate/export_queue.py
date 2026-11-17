"""Write the waiting requests from DATA_DIR/approvals.db back to their JSON queue files, for a rollback to a version without the
approval store (docs/SENDGATE.md, package 3, "Rollback"): external requests to external-outbox.json, and waiting iMessages to
imessage-outbox.json (package 4a; that file is written only when an iMessage waits). It ships in the package, so it runs in the
Docker image:

    docker run --rm -v <your data volume>:/data <this image> python -m icloud_mcp.gate.export_queue /data
    PYTHONPATH=src python -m icloud_mcp.gate.export_queue DATA_DIR          (from a checkout)

Stop the server first: one process holds DATA_DIR at a time. The exported requests end in the store (reason exported) in the same
step, so the JSON file is their only copy. The command prints how many it wrote and what the rollback loses: requests that already
ended, among them unclear ones, and their keys, so a request_id used again after the rollback queues a new request.
Exit codes: 0 written, 1 refused (the server still runs, a request is being carried out, the file exists), 2 wrong arguments.
"""
import sys

from .store import Store


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        stores = [(Store(argv[1], source, ttl=86400, max_items=10**6), name) for source, name in SOURCES]
        stores = [(store, name) for store, name in stores if name == "external-outbox.json" or store.pending()]
        for store, name in stores:                    # refuse before anything is written, so no file is half done
            store.check_export(name)
        done = [(name, store.export_waiting(name)) for store, name in stores]
    except RuntimeError as e:
        print(f"Not exported: {e}", file=sys.stderr)
        return 1
    for name, got in done:
        print(f"Wrote {got['written']} waiting request(s) to {name}. A rollback loses {got['ended_lost']} ended request(s), "
              f"{got['unclear_lost']} of them unclear, and their keys.")
    return 0


SOURCES = (("external", "external-outbox.json"), ("imessage", "imessage-outbox.json"))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
