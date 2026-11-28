"""Service plugins: one kind of plugin for any service the owner connects (a chat network, a mail or calendar provider ...).

A plugin is found through the Python entry point group `icloud_mcp.plugins` (packaging.python.org, "Creating and discovering
plugins"); each entry point names a callable that returns a `Plugin`. The plugins that ship with this package are listed in
`pyproject.toml` under the same group, and also in `BUILTIN`, so a checkout that runs from source without being installed finds
them too. A plugin with the id of one already found is skipped.

A plugin declares data and implementations; the core does the rest. Every callback gets a `PluginContext` (below), never the
server's settings: the plugin reads its own settings with `ctx.value` and its secrets with `ctx.secret`.

- who it is: `id`, `name`, `status` (one line under its name), `limits` (one sentence on what it cannot do), `description` (for
  the Connect card) and `connection` (how it is connected: by whoever runs the server, or later with fields and a sign-in check);
- its settings as data: `settings_keys` (the environment names the owner may change on the dashboard) and `settings_group()` (a
  `settings.Group`, so the dashboard draws it; plugins ship no JavaScript);
- `secret_keys` (environment names that hold secrets: read only through `ctx.secret`, masked in every error, never shown or
  editable on the dashboard), `secrets(ctx)` (further values to mask), `health(ctx)` (an area of icloud_check_health),
  `accounts(ctx)` (what the service page lists: networks, mailboxes ...) and `agent_accounts(ctx)` (its accounts as agents see
  them in icloud_list_accounts, next to the mail, calendar and contacts accounts the core lists itself);
- `api`: the plugin_api version it was written for (1). Import the public names from `icloud_mcp.plugin_api` only.
- the capabilities it offers, each with an interface of its own in the core (below). `messages` is the one in use: reading
  through a `messages.Backend`, sending through a `messages.Sender` with an `actions.ActionType` and an `actions.Executor`,
  finding chats for the recipient picker, and archiving a chat through a `messages.Archiver` (optional). `mail`, `calendar` and `contacts` are interfaces only so far, shaped after what
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
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable

log = logging.getLogger(__name__)

GROUP = "icloud_mcp.plugins"
BUILTIN = {"beeper": "icloud_mcp.plugins.beeper:plugin", "gateway": "icloud_mcp.gate_connectors.actions:plugin"}       # also in pyproject.toml; found here when running from source


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


@dataclass(frozen=True)
class AccountRef:
    """One account of a service as agents see it (icloud_list_accounts): the service's id for it (what `account` takes in the chat
    tools), the network it is on and the owner's name there. Never a password, a key, an address or a phone number."""
    account: str
    network: str = ""
    name: str = ""


# -- what a plugin is handed ---------------------------------------------------------------------------------------------------
API_VERSIONS = frozenset({1})       # the plugin_api versions this core loads (docs/SENDGATE.md, package 7, D7-2)


class PluginContext:
    """What every callback of a plugin gets instead of the server's settings (plugin_api v1, docs/SENDGATE.md package 7):

    - value(key, default): one of the plugin's own settings, the dashboard's value, else the environment's, else `default`;
    - secret(key): one of the plugin's own secret environment names (Plugin.secret_keys), '' when unset; any other name raises
      KeyError;
    - approval_link(kind, id): the link to a request on the owner's dashboard;
    - plugin_id.

    Nothing else of the server: reading any other attribute raises AttributeError. It limits what a plugin is handed, not what
    Python lets it import; a plugin runs in the server's process."""
    __slots__ = ("plugin_id", "_settings", "_keys", "_secret_keys")

    def __init__(self, settings: Any, plugin_id: str, keys: Iterable[str] | None = None, secret_keys: Iterable[str] = ()):
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "_settings", settings)
        object.__setattr__(self, "_keys", None if keys is None else frozenset(keys))
        object.__setattr__(self, "_secret_keys", frozenset(secret_keys))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"PluginContext is read only ({name})")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"PluginContext is read only ({name})")

    def __repr__(self) -> str:
        return f"PluginContext({self.plugin_id!r})"

    def value(self, key: str, default: str = "") -> str:
        if self._keys is not None and key not in self._keys:
            return default                      # another plugin's or the core's setting is not this plugin's to read
        return self._settings.plugin_value(key, default)

    def secret(self, key: str) -> str:
        if key not in self._secret_keys:
            raise KeyError(f"{key} is not one of this plugin's secret names")
        return self._settings.plugin_secret(key)

    def approval_link(self, kind: str, item_id: str) -> str:
        return self._settings.approval_link(kind, item_id)


_contexts: dict[int, tuple[Any, dict[str, PluginContext]]] = {}


def context(plugin: "Plugin", settings: Any) -> PluginContext:
    """The one context of `plugin` on the server with these settings (the same object every time, so a plugin may key what it
    keeps per server on it)."""
    got = _contexts.get(id(settings))
    if got is None or got[0] is not settings:
        got = _contexts[id(settings)] = (settings, {})
    ctx = got[1].get(plugin.id)
    if ctx is None:
        ctx = got[1][plugin.id] = PluginContext(settings, plugin.id, plugin.settings_keys | plugin.env_keys, plugin.secret_keys)
    return ctx


def secrets_of(plugin: "Plugin", settings: Any) -> list[str]:
    """Every value of the plugin that is masked in errors: its secret environment names' values and what `secrets(ctx)` adds."""
    ctx = context(plugin, settings)
    out = [ctx.secret(k) for k in sorted(plugin.secret_keys)]
    try:
        out += [x for x in plugin.secrets(ctx) if x]
    except Exception as e:  # noqa: BLE001  -- masking must not take a tool call down
        log.warning("plugin %s: secrets failed (%s)", plugin.id, type(e).__name__)
    return [x for x in out if x]


# -- actions ----------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    """One kind of action a plugin can carry out after the owner approved it, apart from whether the plugin has chats. Agents ask
    for it with approval_request; the core queues it, shows the card, checks the list and the policy at queue and at approval, and
    calls the executor once. Public from package 1 of docs/SENDGATE.md, versioned in package 7.

    - action_type: how the request looks and which parameters it takes (actions.ActionType). Its first row should name who or
      what the request reaches: the card shows that row first.
    - executor(ctx): what carries it out (actions.Executor), answering sent, failed or unclear.
    - allowed(ctx): who it may reach, as the owner has it now. `*` means anyone, empty means nobody.
    - policy(ctx): further rules on the parameters (a reason a request is not permitted, or None), or None.
    - prepare(ctx, params): asks the service about the target and returns the parameters that are stored, so the card shows the
      service's words. Fields it adds are readOnly in the schema. Raise actions.ExternalError to refuse. Optional.
    - hold(ctx, approving): whether approving is off right now (actions.Hold level "blocked", with the reason the card shows) or the card
      needs a note (level "note"); None otherwise. Read on every look; approving is True just before a release, so the plugin
      may check again then. Optional.
    - recheck(ctx, params): called after the owner approved and before the executor; returns 'target_changed' when the target is
      not what the owner saw (nothing is carried out and the request ends), else None. Optional.
    """
    action_type: Any
    executor: Callable[[PluginContext], Any]
    allowed: Callable[[PluginContext], Iterable[str]] = lambda c: ()
    policy: Callable[[PluginContext], Callable[[Mapping[str, Any]], str | None] | None] = lambda c: None
    prepare: Callable[[PluginContext, Mapping[str, Any]], Mapping[str, Any]] | None = None
    recheck: Callable[[PluginContext, Mapping[str, Any]], str | None] | None = None
    hold: Callable[[PluginContext, bool], Any] | None = None   # (ctx, approving) -> an actions.Hold or None, read every time


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

    Every callback takes the plugin's PluginContext (`ctx`, written `s` below as before).

    - backend(s): a messages.Backend for messages_list_chats, messages_read_chat and messages_search, or None
    - sender(s, external): a messages.Sender that asks the service about the chat and queues the request with the core's
      external approvals (`external.submit(action_type_id, params, requested_by, key=...)`, which answers whether it waits), or None
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
    - archive(s): a messages.Archiver that archives or unarchives a chat by the service's own id, or None when the plugin cannot
      here. Optional. The core offers messages_archive_chat only while this plugin's chats are read here (reading on, so its
      backend joined the hub) and the server is not read only; it refuses a chat the owner keeps from agents before the archiver
      is asked. Archiving is reversible and sends nothing, so it runs without the owner's approval; the core logs it (tool,
      backend, result, never a title).
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
    archive: Callable[[Any], Any] | None = None


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
    mail: Callable[[Any], MailCapability] | None = None          # factories taking the PluginContext; the core uses none of these yet
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
    secret_keys: frozenset[str] = frozenset()      # environment names that hold secrets: ctx.secret only, masked, never on the dashboard
    settings_group: Callable[[], Any] | None = None  # a settings.Group, built when Settings asks
    enabled: Callable[[Any], bool] = lambda s: False   # whether it is set up on this server (gets the PluginContext)
    secrets: Callable[[Any], Iterable[str]] = lambda s: ()   # further values to mask, beyond secret_keys (gets the PluginContext)
    health: Callable[[Any], dict[str, Any]] | None = None   # an area of icloud_check_health: {"ok", ...}; may raise, the core reports it
    accounts: Callable[[Any], Callable[[], Listing]] | None = None   # built once per server; the page calls what it returns
    # its accounts for icloud_list_accounts, as (state, accounts): state "ok" or why nothing could be read ("no_read_key",
    # "unreachable" ...). Never raises; never a password, key, address or number. None: the plugin lists no accounts to agents.
    agent_accounts: Callable[[Any], tuple[str, list[AccountRef]]] | None = None
    actions: tuple[ActionSpec, ...] = ()           # actions of any kind agents may ask for with approval_request (docs/SENDGATE.md)
    api: int | None = None                          # the plugin_api version it was written for; None: written before v1, loaded as v1 with a warning


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
    if plugin.api is None:
        log.warning("plugin %s names no plugin_api version; loaded as version 1", name)
    elif not isinstance(plugin.api, int) or isinstance(plugin.api, bool) or plugin.api not in API_VERSIONS:
        newer = isinstance(plugin.api, int) and not isinstance(plugin.api, bool) and plugin.api > max(API_VERSIONS)
        older = isinstance(plugin.api, int) and not isinstance(plugin.api, bool) and plugin.api < min(API_VERSIONS)
        why = "Needs a newer server" if newer else "Written for an older server" if older else "Names no version this server knows"
        log.warning("plugin %s is written for plugin_api %r; this server loads %s; skipped", name, plugin.api,
                    ", ".join(map(str, sorted(API_VERSIONS))))
        _skipping[plugin.id] = (plugin.name, why)
        return None
    return plugin


_skipping: dict[str, tuple[str, str]] = {}     # filled while one set of plugins loads: id -> (name, why)
_skipped: dict[tuple, dict[str, tuple[str, str]]] = {}   # per loaded set (the cache key of _loaded), for Connect


def skipped() -> list[dict[str, str]]:
    """The plugins this server did not load because of their plugin_api version, as Connect a service lists them."""
    load_plugins()
    got = _skipped.get(_key(), {})
    return [{"id": i, "name": n, "why": w} for i, (n, w) in sorted(got.items())]


def entry_points() -> list[tuple[str, str]]:
    """(name, "module:attr") of every plugin entry point installed. Tests replace this to add plugins of their own."""
    return [(ep.name, ep.value) for ep in importlib.metadata.entry_points(group=GROUP)]


def load_plugins() -> list[Plugin]:
    """Every plugin, the built-in ones first, each id once."""
    return list(_loaded(*_key()))     # a changed connector setup builds the plugins again


def _key() -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    marker = tuple(os.environ.get(k, "") for k in ("GATE_ACTIONS_FILE", "GATE_GATEWAY_URL", "GATE_EXEC_KEY", "GATE_AGENT_KEY_PROBE",
                                                   "GATE_PROBE_REQUIRED"))
    return (*BUILTIN.items(), *entry_points()), marker


@functools.lru_cache(maxsize=8)
def _loaded(targets: tuple[tuple[str, str], ...], marker: tuple[str, ...] = ()) -> tuple[Plugin, ...]:
    _skipping.clear()
    out: dict[str, Plugin] = {}
    for name, target in targets:
        if name in out:
            continue
        p = _load(name, target)
        if p is not None and p.id not in out:
            out[p.id] = p
    _skipped[(targets, marker)] = dict(_skipping)
    return tuple(out.values())


def enabled_plugins(s: Any) -> list[Plugin]:
    out = []
    for p in load_plugins():
        try:
            if p.enabled(context(p, s)):
                out.append(p)
        except Exception as e:  # noqa: BLE001
            log.warning("plugin %s: could not tell whether it is set up (%s)", p.id, type(e).__name__)
    return out


def env_keys() -> frozenset[str]:
    return frozenset(k for p in load_plugins() for k in p.env_keys)


def secret_keys() -> frozenset[str]:
    """Every plugin's secret environment names: read into Settings apart from the plugin values, never editable."""
    return frozenset(k for p in load_plugins() for k in p.secret_keys)


def settings_keys() -> frozenset[str]:
    """The environment names every plugin lets the owner change on the dashboard."""
    return frozenset(k for p in load_plugins() for k in p.settings_keys)


def settings_groups() -> list[Any]:
    out = []
    for p in load_plugins():
        if p.settings_group is not None:
            out.append(p.settings_group())
    return out
