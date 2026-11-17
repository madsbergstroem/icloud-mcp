"""Whether agents could carry out a file action without the owner (docs/SENDGATE.md, E1, D3, E-14).

GATE_AGENT_KEY_PROBE holds one or more keys (comma-separated) with the grants agents have at the gateway. A probe run lists the
tools each key can see, once per key, and gives every action one state:

- "bypassable": a probe key lists the action's `tool`. Approving is off (D3) until the grant is gone and a later run is clean.
- "unchecked": no probe key is set, no run has finished yet, or a key could not list its tools. A note only, unless
  GATE_PROBE_REQUIRED is true: then it blocks like "bypassable" until a run is clean.
- "ok": every probe key listed its tools and none of them lists the action's `tool`.

A run happens in the background when the server starts, on every icloud_check_health call, and just before an approval when
the last run is older than MAX_AGE (so a grant added since cannot slip through); cards read the last result. Only
listing is checked, not calling: a gateway that hides a tool from the list but still allows the call passes this check.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from ..gate.actions import Hold

log = logging.getLogger(__name__)

BLOCKED = "An agent key can call this tool directly. Whoever runs the server removes that grant in the gateway first."
NOTE = {"no_key": "Not checked whether agents can call this tool without you: no probe key is set.",
        "pending": "Not checked yet whether agents can call this tool without you.",
        "unreachable": "Not checked whether agents can call this tool without you: the gateway did not answer."}
ORDER = {"problem": 0, "warning": 1, "ok": 2}
FRESH = 10.0            # seconds: a caller that waited for a run in progress takes its result instead of starting another
MAX_AGE = 600.0         # seconds: an approval after this long checks again first
REQUIRED = "Approving is off until whoever runs the server lets the check run: GATE_PROBE_REQUIRED is on." 


@dataclass(frozen=True)
class State:
    state: str          # ok, bypassable, unchecked
    why: str = ""       # for unchecked: no_key, pending, unreachable


class Probe:
    """`client(key)` gives a gateway client for one key (a Gateway with list_tools); `actions` are (action id, tool) pairs."""

    def __init__(self, keys: Sequence[str], actions: Sequence[tuple[str, str]], client: Callable[[str], Any], required: bool = False):
        self.keys, self.actions, self.client, self.required = [k for k in keys if k], list(actions), client, required
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._states = {a: State("unchecked", "pending" if self.keys else "no_key") for a, _ in self.actions}
        self.checked_at: float | None = None

    def state(self, action: str) -> State:
        with self._lock:
            return self._states.get(action, State("unchecked", "pending"))

    def _list(self, key: str) -> set[str] | None:
        try:
            return set(self.client(key).list_tools())
        except Exception as e:  # noqa: BLE001  -- a key that cannot list is a key not checked; nothing of the error is kept
            log.warning("A probe key could not list the gateway's tools (%s)", type(e).__name__)
            return None

    def run(self) -> None:
        """One run: one list_tools per key, the keys at once, so a run takes as long as the slowest key. Runs do not overlap;
        a caller that waited for a run in progress takes its result."""
        if not self.keys:
            return
        asked = time.time()
        with self._run_lock:
            if self.checked_at is not None and self.checked_at >= asked - FRESH:
                return
            with ThreadPoolExecutor(max_workers=len(self.keys), thread_name_prefix="gate-probe-key") as pool:
                got = list(pool.map(self._list, self.keys))
            seen = [s for s in got if s is not None]
            failed = len(seen) < len(got)
            out = {}
            for action, tool in self.actions:
                before = self.state(action).state
                if any(tool in s for s in seen):
                    out[action] = State("bypassable")
                elif failed and before == "bypassable":     # only a run where every key listed clears it (D3)
                    out[action] = State("bypassable")
                elif failed:
                    out[action] = State("unchecked", "unreachable")
                else:
                    out[action] = State("ok")
            with self._lock:
                self._states, self.checked_at = out, time.time()

    def start(self) -> None:
        """A first run in the background, so the start never waits on the gateway."""
        if self.keys:
            threading.Thread(target=self.run, name="gate-probe", daemon=True).start()

    def hold(self, action: str, approving: bool = False) -> Hold | None:
        if approving and self.keys and (self.checked_at is None or time.time() - self.checked_at > MAX_AGE):
            self.run()                                # an old look is not enough to approve on
        st = self.state(action)
        if st.state == "bypassable":
            return Hold("blocked", "bypassable", BLOCKED)
        if st.state == "unchecked":
            return Hold("blocked", "unchecked", f"{NOTE[st.why]} {REQUIRED}") if self.required else Hold("note", "unchecked", NOTE[st.why])
        return None

    def lines(self, titles: dict[str, str]) -> list[dict[str, str]]:
        """One line per action for the health report, problems first, then warnings, then ok."""
        out = []
        for action, tool in self.actions:
            st = self.state(action)
            level = {"bypassable": "problem", "unchecked": "warning"}.get(st.state, "ok")
            text = {"problem": f"An agent key can call {tool}. Remove that grant in the gateway.",
                    "warning": NOTE.get(st.why, NOTE["pending"]), "ok": "Agents cannot call it without you."}[level]
            out.append({"name": titles.get(action, action), "action": action, "level": level, "text": text})
        return sorted(out, key=lambda x: ORDER[x["level"]])
