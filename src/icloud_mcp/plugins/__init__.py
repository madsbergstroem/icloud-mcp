"""Service plugins: one kind of plugin for any service the owner connects (a chat network, a mail or calendar provider ...).

A plugin is found through the Python entry point group `icloud_mcp.plugins` (packaging.python.org, "Creating and discovering
plugins"); each entry point names a callable that returns a `Plugin`. The plugins that ship with this package are listed in
`pyproject.toml` under the same group, and also in `BUILTIN`, so a checkout that runs from source without being installed finds
them too. A plugin with the id of one already found is skipped.

A plugin declares data and implementations; the core does the rest:

- who it is: `id`, `name`, `status` (one line under its name), `limits` (one sentence on what it cannot do), `description` (for
  the Connect card) and `connection` (how it is connected: by whoever runs the server, or later with fields and a sign-in check);
- its settings as data: `settings_keys` (the environment names the owner may change on the dashboard) and `settings_group()` (a
  `settings.Group`, so the dashboard draws it; plugins ship no JavaScript);
- `secrets(s)` (values masked in every error), `health(s)` (an area of icloud_check_health) and `accounts(s)` (what the
  service page lists: networks, mailboxes ...);
- the capabilities it offers, each with an interface of its own in the core (below). `messages` is the one in use: reading
  through a `messages.Backend`, sending through a `messages.Sender` with an `actions.ActionType` and an `actions.Executor`, and
  finding chats for the recipient picker. `mail`, `calendar` and `contacts` are interfaces only so far, shaped after what
  MailService, CalendarService and ContactsService do, so the IMAP, CalDAV and CardDAV code can become their first plugin.

The guarantees stay in the core and a plugin cannot get round them: a request to send is queued with its canonical hash, the
owner approves it, the core calls the plugin's executor once, the allowlist and the plugin's policy are checked when a request is
queued and again when it is approved (external.py), and the log holds metadata only.
"""
from __future__ import annotations

import functools
import importlib
import importlib.metadata
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable

log = logging.getLogger(__name__)

GROUP = "icloud_mcp.plugins"
BUILTIN = {"beeper": "icloud_mcp.plugins.beeper:plugin"}       # also in pyproject.toml; found here when running from source


# -- how a service is connected -----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str = "text"          # text | password | email | url
    placeholder: str = ""


@dataclass(frozen=True)
class Connection:
    """"operator": set up by whoever runs the server (environment), nothing to connect on the dashboard. "form": the owner connects it
    with `fields`, proven by `check(settings, values)` before anything is saved (an error text, or None). The core draws "form" on
    Connect a service once a plugin uses it."""
    kind: str = "operator"
    fields: tuple[Field, ...] = ()
    check: Callable[[Any, Mapping[str, str]], str | None] | None = None


@dataclass(frozen=True)
class Listing:
    """What a service page lists (networks, mailboxes ...): a heading, the rows, and a problem when it could not be read."""
    label: str
    items: list[dict[str, str]] = field(default_factory=list)      # each {"title", "sub", and optionally "detail"}
    problem: str = ""
    note: str = ""
    state: str = "ok"           # the plugin's own word for how reading went ("ok", "unreachable" ...), for logs and tests


# -- capabilities ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ChatRef:
    """A chat as the recipient picker shows it. `found` False: the service does not know this id (any more)."""
    id: str
    title: str = ""
    network: str = ""
    group: bool = False
    people: int | None = None
    last_activity: str = ""
    found: bool = True


@dataclass
class MessagesCapability:
    """Chats on a messaging service. `prefix` is what its chat ids start with in the chat tools (`beeper:`). Each part is optional:
    a plugin may only read, only send, or both.

    - backend(s, hub_filters): a messages.Backend for messages_list_chats, messages_read_chat and messages_search, or None
    - sender(s, external): a messages.Sender that asks the service about the chat and queues the request with the core's
      external approvals (`external.queue_external(action_type_id, params, requested_by, key=...)`), or None
    - action_type, executor(s): what is queued, and what carries it out after the owner approved; the core calls it once
    - allowed(s): the owner's list of who may be reached (`*` for anyone), checked at queue and at approval
    - policy(s): further rules on the parameters (a reason a request is not permitted, or None), checked the same way
    - search_chats(s, query) / resolve(s, ids): the recipient picker, as (state, chats): state "ok" or why nothing could be read
      ("no_read_key", "unreachable" ...); they never raise. An empty query gives the most recent chats.
    - networks(s): the networks the picker offers for the network rule (names as the chats carry them)
    - recipients_key / networks_key / groups_key: the plugin's settings the core's picker edits (its allowlist, `*` for any chat;
      the networks that may be written to, `*` for all; whether group chats may be written to). Empty: that part is not offered.
    - sending_on(s): whether sending is set up at all
    - reading_on(s): whether the owner has reading on (backend(s) then gives a Backend)
    - reading_keys: the plugin's reading settings the core's picker edits, by role: "on" (the switch), "hidden" and "visible"
      (chat lists in the shape of recipients_key), "max_age" (days), "short_codes" (hide service senders). Empty: not offered.
    """
    prefix: str
    action_type: Any = None
    executor: Callable[[Any], Any] | None = None
    sender: Callable[[Any, Any], Any] | None = None
    backend: Callable[[Any], Any] | None = None
    allowed: Callable[[Any], Iterable[str]] = lambda s: ()
    policy: Callable[[Any], Callable[[Mapping[str, Any]], str | None] | None] = lambda s: None
    search_chats: Callable[[Any, str], tuple[str, list[ChatRef]]] = lambda s, q: ("ok", [])
    resolve: Callable[[Any, list[str]], tuple[str, list[ChatRef]]] = lambda s, ids: ("ok", [ChatRef(i, found=False) for i in ids])
    networks: Callable[[Any], list[str]] = lambda s: []
    recipients_key: str = ""
    networks_key: str = ""
    groups_key: str = ""
    sending_on: Callable[[Any], bool] = lambda s: False
    reading_on: Callable[[Any], bool] | None = None
    reading_keys: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class MailCapability(Protocol):
    """Interface only, for a later mail plugin (the IMAP and SMTP code of mail.py as its first user): read and search folders,
    fetch messages, and send through the core's approval like mail_send_message does today."""
    def list_folders(self) -> list[dict[str, Any]]: ...
    def search_messages(self, folder: str, query: Mapping[str, Any], limit: int) -> list[dict[str, Any]]: ...
    def get_message(self, folder: str, uid: str) -> dict[str, Any]: ...
    def send_raw(self, raw: bytes, recipients: list[str]) -> dict[str, Any]: ...


@runtime_checkable
class CalendarCapability(Protocol):
    """Interface only, shaped after CalendarService: list calendars, list events in a range, and write events (sent to guests only
    after approval, as calendar invitations are today)."""
    def list_calendars(self) -> list[dict[str, Any]]: ...
    def list_events(self, calendar_id: str, start: str, end: str) -> list[dict[str, Any]]: ...
    def put_event(self, calendar_id: str, ical: bytes) -> dict[str, Any]: ...


@runtime_checkable
class ContactsCapability(Protocol):
    """Interface only, shaped after ContactsService: search, read and write contact cards."""
    def search_contacts(self, query: str, limit: int) -> list[dict[str, Any]]: ...
    def get_contact(self, contact_id: str) -> dict[str, Any]: ...
    def put_contact(self, vcard: str) -> dict[str, Any]: ...


@dataclass
class Capabilities:
    messages: MessagesCapability | None = None
    mail: Callable[[Any], MailCapability] | None = None          # factories taking the settings; the core uses none of these yet
    calendar: Callable[[Any], CalendarCapability] | None = None
    contacts: Callable[[Any], ContactsCapability] | None = None

    def names(self) -> list[str]:
        return [n for n in ("messages", "mail", "calendar", "contacts") if getattr(self, n) is not None]


# -- the plugin -----------------------------------------------------------------------------------------------------------------
@dataclass
class Plugin:
    id: str
    name: str
    status: str                                     # the grey line under its name: "Set up by whoever runs this server"
    limits: str                                     # one sentence on what it cannot do
    description: str                                # the Connect card's text
    connection: Connection = field(default_factory=Connection)
    capabilities: Capabilities = field(default_factory=Capabilities)
    settings_keys: frozenset[str] = frozenset()     # the environment names the owner may change on the dashboard
    env_keys: frozenset[str] = frozenset()          # names only whoever runs the server sets; read like settings_keys, never editable
    settings_group: Callable[[], Any] | None = None  # a settings.Group, built when Settings asks
    enabled: Callable[[Any], bool] = lambda s: False   # whether it is set up on this server
    secrets: Callable[[Any], Iterable[str]] = lambda s: ()
    health: Callable[[Any], dict[str, Any]] | None = None   # an area of icloud_check_health: {"ok", ...}; may raise, the core reports it
    accounts: Callable[[Any], Callable[[], Listing]] | None = None   # built once per server; the page calls what it returns


def _load(name: str, target: str) -> Plugin | None:
    module, _, attr = target.partition(":")
    try:
        got = getattr(importlib.import_module(module), attr)
        plugin = got() if callable(got) and not isinstance(got, Plugin) else got
    except Exception as e:  # noqa: BLE001  -- one broken plugin must not take the server down
        log.warning("plugin %s could not be loaded (%s)", name, type(e).__name__)
        return None
    if not isinstance(plugin, Plugin):
        log.warning("plugin %s is not a Plugin; skipped", name)
        return None
    return plugin


def entry_points() -> list[tuple[str, str]]:
    """(name, "module:attr") of every plugin entry point installed. Tests replace this to add plugins of their own."""
    return [(ep.name, ep.value) for ep in importlib.metadata.entry_points(group=GROUP)]


def load_plugins() -> list[Plugin]:
    """Every plugin, the built-in ones first, each id once."""
    return list(_loaded((*BUILTIN.items(), *entry_points())))


@functools.lru_cache(maxsize=8)
def _loaded(targets: tuple[tuple[str, str], ...]) -> tuple[Plugin, ...]:
    out: dict[str, Plugin] = {}
    for name, target in targets:
        if name in out:
            continue
        p = _load(name, target)
        if p is not None and p.id not in out:
            out[p.id] = p
    return tuple(out.values())


def enabled_plugins(s: Any) -> list[Plugin]:
    out = []
    for p in load_plugins():
        try:
            if p.enabled(s):
                out.append(p)
        except Exception as e:  # noqa: BLE001
            log.warning("plugin %s: could not tell whether it is set up (%s)", p.id, type(e).__name__)
    return out


def env_keys() -> frozenset[str]:
    return frozenset(k for p in load_plugins() for k in p.env_keys)


def settings_keys() -> frozenset[str]:
    """The environment names every plugin lets the owner change on the dashboard."""
    return frozenset(k for p in load_plugins() for k in p.settings_keys)


def settings_groups() -> list[Any]:
    out = []
    for p in load_plugins():
        if p.settings_group is not None:
            out.append(p.settings_group())
    return out
