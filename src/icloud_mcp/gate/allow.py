"""gate.allow: an agent asks the owner to add one person or chat to one of the owner's lists (docs/SENDGATE.md, package 10,
"Adding someone to a list on request").

A list entry only widens who may be reached at all; every message to the new entry still waits for the owner's approval. The
owner approves the entry on the dashboard like any other request, with no fresh sign-in (the owner, 2026-11-27).

Which lists: a settings `Spec` with `requestable` set (settings.py and a plugin's own group). What the card shows comes from the
fields and from the service (`Spec.entry_info`, for Beeper the chat's name), never from the agent's words; the agent's reason is
shown as its own text and cannot be edited.

What approving does: the entry is added to the value in effect (dashboard, environment or default) and written with the settings
store, the path a dashboard save takes. It applies at once on this server (config.add_live), so the agent can ask to write to the
new entry without a restart; removing an entry stays in the settings. Where an entry came from is kept in `list-origins.json` in
the data folder, for the settings page to show "on request".
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Mapping

from . import actions as A

log = logging.getLogger(__name__)

ACTION_ID = "gate.allow"
ORIGINS_FILE = "list-origins.json"
MAX_ENTRY = 200
MAX_REASON = 500
MAX_ORIGINS = 500
_LOCK = threading.Lock()


def _entries(value: str) -> list[str]:
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


def requestable(specs: Mapping[str, Any]) -> dict[str, Any]:
    """The lists an agent may ask an entry for, by key, in a stable order."""
    return {k: sp for k, sp in sorted(specs.items()) if getattr(sp, "requestable", False) and sp.kind == "list"}


def schema(lists: Mapping[str, Any]) -> dict[str, Any]:
    which = "; ".join(f"{k}: {sp.label}" for k, sp in lists.items())
    return {
        "type": "object", "additionalProperties": False,
        "required": ["setting", "entry", "reason", "list_label", "entry_label"],
        "properties": {
            "setting": {"type": "string", "enum": list(lists), "description": f"The list, by key. {which}."},
            "entry": {"type": "string", "minLength": 1, "maxLength": MAX_ENTRY,
                      "description": "One person or chat as the list holds it: an email address, a phone number or a chat id. "
                                     "Never *, a domain or several at once."},
            "reason": {"type": "string", "minLength": 1, "maxLength": MAX_REASON,
                       "description": "Why, in one sentence the owner reads on the card."},
            "list_label": {"type": "string", "readOnly": True},
            "list_group": {"type": "string", "readOnly": True},
            "entry_label": {"type": "string", "readOnly": True},
            "entry_id": {"type": "string", "readOnly": True},
        },
    }


def _warnings(p: Mapping[str, Any]) -> list[str]:
    """iMessage compares phone numbers by their last nine digits (imessage._key), so the card says what else such an entry lets in."""
    digits = "".join(ch for ch in str(p.get("entry") or "") if ch.isdigit())
    if p.get("setting") == "IMESSAGE_SEND_ALLOWLIST" and "@" not in str(p.get("entry") or "") and len(digits) > 9:
        return [f"Your iMessage list matches phone numbers by their last nine digits, so this also lets in any number ending in {digits[-9:]}."]
    return []


def action_type(lists: Mapping[str, Any]) -> A.ActionType:
    return A.ActionType(
        id=ACTION_ID, title="Add to list", badge="Add to list", approve_label="Add", params_schema=schema(lists),
        rows=(("List", "list_label"), ("Adds", "entry_label"), ("ID", "entry_id")),
        text_field="reason", text_caption="Your assistant's words",
        text_locked=lambda p: "Your assistant's reason stays as it was written.",
        subject=lambda p: f"{p.get('list_group') or 'List'} · {p.get('entry_label') or ''}".strip(" ·"),
        warnings=_warnings,
        row_tags=lambda p: {"List": str(p.get("list_group") or "")} if p.get("list_group") else {},
        note="Your assistant may then ask to reach them. Each message still waits for your approval. "
             "You can remove the entry in Settings at any time.")


class ListAllow:
    """The policy, the details for the card and the executor of gate.allow for one server.

    settings: the server's Settings (read for entry_info, changed in place by config.add_live); store: the SettingsStore;
    specs: () -> {key: Spec}, read each time so a plugin's group counts; current: (key, stored) -> (value, source)."""

    def __init__(self, s: Any, store: Any, specs: Callable[[], Mapping[str, Any]], current: Callable[[str, dict[str, str]], tuple[str, str]],
                 add_live: Callable[[Any, str, str], None], groups: Callable[[], list[Any]] | None = None):
        self.s, self.store, self.specs, self.current, self.add_live = s, store, specs, current, add_live
        self.groups = groups or (lambda: [])
        self.path = os.path.join(s.data_dir, ORIGINS_FILE)

    # -- what may be asked for -------------------------------------------------------------------------------------------
    def lists(self) -> dict[str, Any]:
        return requestable(self.specs())

    def _group_of(self, key: str) -> str:
        return next((g.title for g in self.groups() if any(sp.key == key for sp in g.specs)), "")

    def _value(self, key: str) -> str:
        return self.current(key, self.store.read())[0]

    def _covered(self, sp: Any, entry: str, value: str) -> bool:
        """Whether the list as it is now already lets this entry through."""
        have = [x.casefold() for x in _entries(value)]
        if "*" in have:
            return True
        if sp.empty_means_anyone and not have:
            return True
        want = entry.casefold()
        if want in have:
            return True
        dom = want.rpartition("@")[2]
        return "@" in want and any(h.lstrip("@") == dom and "@" not in h.lstrip("@") for h in have)

    @staticmethod
    def _shape(entry: str) -> str | None:
        e = entry.strip()
        if not e or e == "*" or "," in e or e.startswith("@") or "*" in e:
            return "One request adds one person or chat: not *, a domain or several at once."
        return None

    def prepare(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """The agent's setting, entry and reason, with what the card shows filled in from the settings and the service.
        Refuses (policy) what this list cannot take or already holds."""
        key, entry = str(params.get("setting") or ""), str(params.get("entry") or "").strip()
        sp = self.lists().get(key)
        if sp is None:
            raise A.ExternalError("This list can only be changed by the owner in Settings.", "policy", "params/setting")
        if why := self._shape(entry):
            raise A.ExternalError(why, "policy", "params/entry")
        label, problem, canonical = entry, None, entry
        if sp.entry_info is not None:
            try:
                info = sp.entry_info(self.s, entry) or {}
            except Exception as e:  # noqa: BLE001  -- the service's own error text stays here
                log.warning("entry_info for %s raised %s", key, type(e).__name__)
                raise A.ExternalError("The service could not be asked about this entry. Nothing was queued; try again in a minute.",
                                      "describe_failed") from None
            label, problem = str(info.get("label") or entry), info.get("problem")
            canonical = str(info.get("entry") or entry)
        if problem:
            raise A.ExternalError(str(problem), "policy", "params/entry")
        value = self._value(key)
        if sp.empty_means_anyone and not _entries(value):
            raise A.ExternalError("Already allowed: this list is empty, which means anyone.", "policy", "params/entry")
        if self._covered(sp, canonical, value):
            raise A.ExternalError("Already allowed: this entry is on the list.", "policy", "params/entry")
        return {"setting": key, "entry": canonical, "reason": str(params.get("reason") or ""), "list_label": sp.label,
                "list_group": self._group_of(key), "entry_label": label, **({"entry_id": canonical} if label != canonical else {})}

    def policy(self, params: Mapping[str, Any]) -> str | None:
        """Checked when queued and again when approved: the list may still be asked for, and the entry is one person or chat."""
        if str(params.get("setting") or "") not in self.lists():
            return "This list can only be changed by the owner in Settings."
        return self._shape(str(params.get("entry") or ""))

    def recheck(self, params: Mapping[str, Any]) -> Any:
        """Just before the entry is added, the service is asked again (a Beeper chat): one that can no longer be added ends the
        request; one whose name changed waits again as a new version with the service's words; one that cannot be asked waits."""
        key, entry = str(params.get("setting") or ""), str(params.get("entry") or "").strip()
        sp = self.lists().get(key)
        if sp is None:
            raise A.Rechecked("not_allowed", "Not added: this list can only be changed by you in Settings.")
        if sp.entry_info is None:
            return None
        try:
            info = sp.entry_info(self.s, entry) or {}
        except Exception as e:  # noqa: BLE001  -- the service's own error text stays here
            log.warning("entry_info for %s raised %s at approval", key, type(e).__name__)
            raise A.ExternalError("Not added: the service could not be asked about this entry. Try again in a moment.") from None
        if info.get("problem"):
            raise A.Rechecked("target_changed", f"Not added: {info['problem']}")
        if str(info.get("entry") or entry) != entry:
            return "target_changed"
        label = str(info.get("label") or entry)
        if label != str(params.get("entry_label") or ""):
            return {**params, "entry_label": label}
        return None

    # -- approving -------------------------------------------------------------------------------------------------------
    def execute(self, request: A.ActionRequest) -> A.Result:
        p = request.params
        key, entry = str(p.get("setting") or ""), str(p.get("entry") or "").strip()
        sp = self.lists().get(key)
        if sp is None or self._shape(entry):
            return A.Result("failed", "Not added: this list cannot take it any more.")
        added: list[bool] = []

        def add(stored: dict[str, str]) -> dict[str, str]:      # under the settings file's lock: the list as it is now
            value = self.current(key, stored)[0]
            if self._covered(sp, entry, value):
                return {}
            added.append(True)
            return {key: ",".join(_entries(value) + [entry])}
        try:
            self.store.change(add)
        except OSError as e:
            log.warning("gate.allow could not write the settings (%s)", type(e).__name__)
            return A.Result("failed", "Not added: the settings could not be written. It waits again.")
        if added:
            try:
                with _LOCK:
                    self._remember(key, entry, str(p.get("entry_label") or entry), request.id)
            except OSError as e:                    # the entry is saved; only the "On request" note is missing
                log.warning("gate.allow could not note where %s came from (%s)", key, type(e).__name__)
        try:
            self.add_live(self.s, key, entry)       # also when it was on disk already: a running server catches up
        except Exception as e:  # noqa: BLE001  -- written: it applies after the next restart
            log.warning("gate.allow could not apply %s at once (%s)", key, type(e).__name__)
            return A.Result("sent", f"Added to {sp.label}. It applies after a restart.")
        return A.Result("sent", f"Added to {sp.label}." if added else f"Already allowed: nothing to add to {sp.label}.")

    # -- where entries came from -----------------------------------------------------------------------------------------
    def _read_origins(self) -> list[dict[str, Any]]:
        return _read(self.path)

    def _remember(self, key: str, entry: str, label: str, item_id: str) -> None:
        from ..admin import _write_private
        rows = [o for o in self._read_origins() if not (o.get("setting") == key and str(o.get("entry", "")).casefold() == entry.casefold())]
        rows.append({"setting": key, "entry": entry, "label": label[:MAX_ENTRY], "item_id": item_id, "at": int(time.time())})
        _write_private(self.path, json.dumps(rows[-MAX_ORIGINS:], indent=1) + "\n")



def _read(path: str) -> list[dict[str, Any]]:
    try:
        with open(path) as f:
            got = json.load(f)
    except (OSError, ValueError):
        return []
    return [o for o in got if isinstance(o, dict)] if isinstance(got, list) else []


def origins(data_dir: str, key: str, value: str) -> list[dict[str, Any]]:
    """The entries of this list, as it is now, that came from an approved request: entry, label, item_id, at. An entry no longer
    on the list shows none."""
    have = {x.casefold() for x in _entries(value)}
    return [{k: o.get(k) for k in ("entry", "label", "item_id", "at")} for o in _read(os.path.join(data_dir, ORIGINS_FILE))
            if o.get("setting") == key and str(o.get("entry", "")).casefold() in have]


def register(registry: A.ActionRegistry, allow: ListAllow) -> bool:
    """gate.allow on the external source, when at least one list may be asked for. True when it was registered."""
    lists = allow.lists()
    if not lists:
        return False

    registry.register(action_type(lists), allow, ["*"], allow.policy, prepare=allow.prepare, recheck=allow.recheck, builtin=True)
    return True
