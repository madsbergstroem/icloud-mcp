# Sendgate: one approval gate for what an agent sends

Status: reviewed and approved 2026-11-13 (see "Review obligations"). "Sendgate" is the working title the dashboard already
shows. Packages 0 to 4 and 5a are built; the others are not.
"Current state" was read at commit `330c82f` (branch `mb/ionic-ui`, renamed `mb/sendgate` on 2026-11-18), which left upstream at `184ffa8`.

## Context

This server lets an agent ask to send something and lets only the owner release it. That started with mail and grew to calendar
invitations, iMessages and, in this fork, Beeper chats. The owner now wants the same gate in front of other things an agent does
on their behalf: a reply on a marketplace, a booking, a post. Each of those sits behind an MCP gateway today, and each has its
own way of asking "are you sure".

The gate's rules hold up. The seam around it does not yet: a new service needs Python inside this process and edits to the
core, and three of the four kinds of request do not use the general path.

## Order: build in the fork, then propose

Every package is built and run in this fork first. Only once the gate works here does the fork's owner ask the upstream author
whether it belongs in iCloud MCP. The plan has to survive either answer:

- If upstream takes it, the gate becomes a package inside iCloud MCP, and mail, calendar and iMessage are its first connectors.
- If upstream declines, the gate moves to a repository of its own, and iCloud MCP (upstream, unchanged) becomes one service it
  can sit in front of.

Both need the same first step: a line through the code that says what is the gate and what is iCloud. Package 0 draws that
line, and every later package stays on one side of it.

## Current state

What already holds, and stays as it is:

- A request is stored as the canonical JSON of `{action_type, params}`; the hash covers exactly those bytes, so an edit by the
  owner changes it and an approval of the old version is refused (`src/icloud_mcp/actions.py:175`, `external.py:206`).
- The owner's list of who may be reached and the type's policy are checked when a request is queued and again when it is approved
  (`external.py:145`, `external.py:202`).
- An executor answers `sent`, `failed` or `unclear`. Only `failed` puts the request back. An executor that raises counts as
  `unclear` (`external.py:210-224`).
- The activity log carries a hash chain and never a text or a parameter (`audit.py`).
- Made-up plugins in the tests (Chatty, Mailbox, Parlor) join without a core change, and golden files pin the stored bytes.

Where it does not carry:

| # | Gap | Evidence |
|---|-----|----------|
| 1 | An action type can only be registered through the `messages` capability, so a request that is not a chat message has no way in | `server.py:3011-3017` |
| 2 | The one tool that queues such a request is `messages_send_request`, with parameters marked "Beeper only" | `server.py:3035-3040` |
| 3 | Four queues and four `ApprovalSource` classes; only Beeper uses `ActionType` and `Executor` | `dashboard.py:204`, `:295`, `:360`, `external.py:44` |
| 4 | Code branches on the kind of request in 4 Python files and 1 script | `approval_withdraw.py:53-59`, `dashboard_api.py:93`, `:197`, `:271-294`, `textedit.py:155`, `dashboard.py:888`, `static/app/today.js:25-28`, `:218-225` |
| 5 | A plugin is typed with `Any`, gets the core's `Settings` object and imports private names from `settings` | `plugins/__init__.py:119-134`, `plugins/beeper/__init__.py:37` |
| 6 | Beeper's settings are fields of the core's configuration | `config.py:236-243` |
| 7 | The queue is one JSON file rewritten on every change, with no state for "being carried out". A crash between `claim` and the executor's answer loses the request | `outbox.py:87-97`, `:164-171`, `external.py:203-211` |
| 8 | An idempotency key is compared with waiting requests only, so a repeat after the owner decided queues the request again | `external.py:175` |
| 9 | The log lines of a request do not carry its hash, so the log cannot say which version was approved | `textedit.py:224-226`, `dashboard_api.py:334`, `approval_status.py:103` |
| 10 | External requests need the dashboard; `/outbox` serves mail and iMessage only | `server.py:3014`, `approvals.py:102` |

`docs/DESIGN.md` ("Where the seam is not general yet") already names 4 and 10.

## Provenance: what belongs where

Since the fork left upstream, 205 files were added, 37 changed and 1 removed. Each tracked file gets exactly one of four bins:

| Bin | Meaning | What happens to it |
|-----|---------|--------------------|
| `upstream` | iCloud MCP as its author ships it | Untouched, or changed only by a commit of the `mergeable` bin |
| `mergeable` | A change that makes sense for iCloud MCP whether or not it takes the gate | Offered upstream as a pull request of its own |
| `gate` | Sendgate: queue, registry, approval pages, status, withdrawal, log | Becomes a package upstream, or its own repository |
| `connector` | One service behind the gate | A plugin; ships with whoever owns the gate |

A first sort of `src/icloud_mcp`. Which `mergeable` candidates go upstream is for the upstream author to decide.

| Bin | Files |
|-----|-------|
| `gate`, new in the fork | `actions.py`, `external.py`, `approval_status.py`, `approval_withdraw.py`, `push.py`, `dashboard_api.py`, `plugins/__init__.py`, `hub_accounts.py`, `static/app/*` |
| `gate`, grown from upstream files | `outbox.py` (+75 −12), `audit.py` (unchanged), `static/dashboard.css`, `static/sw.js` |
| `connector` | `plugins/beeper/*`, `beeper.py`, `messages.py` (the chat model that connectors share) |
| `mergeable` candidates | `accounts.py`, `dashboard_accounts.py`, `discover.py`, `setup_page.py`, `access_sso.py`, and the fork's changes to `mail.py` (+169 −16), `imessage.py` (+112 −12), `auth.py` (+74 −9), `cal.py`, `passkeys.py`, `callctx.py`, `contacts.py` |
| mixed, to be split | `server.py` (+340 −134), `config.py` (+126 −17), `settings.py` (+130 −297), `instructions.py` (+29 −13), `admin.py` (+34 −13), `dashboard.py` (+293 −642, the approval sources of mail, calendar and iMessage next to the gate's pages), `approvals.py` (+29 −10, the `/outbox` pages for mail and iMessage), `textedit.py` (new; editing for every kind, with mail and calendar parts) |
| stays in the fork | `feedbackkit.py` (off by default) |

Package 0 turns this table into a file the tests read, so the sort stays true as the code moves.

## Target design

```
agent ──ask──> tool layer ──queue──> gate ──card──> owner
                                      │               │ approve / edit / discard
                                      │ <─────────────┘
                                      └──execute once──> connector ──> service
```

| Layer | Holds | Knows about |
|-------|-------|-------------|
| Gate | Store, action registry, checks at queue and at approval, approval pages, status, withdrawal, log, push | Action types and executors. No mail, calendar, chat or gateway |
| Connector | Action types, executors, what the card shows, the owner's lists for its service | The gate's public interface and its own service |
| Tool layer | MCP tools that turn an agent's call into a queued request | The gate and the connectors that are set up |

Rules every package keeps:

- One instance per owner, with its own `DATA_DIR`.
- An agent or an outside system can only ask. The owner approves. The server carries it out, once.
- `failed` means the other side provably never got the request and is the only answer that puts it back. `unclear` is never
  repeated.
- Log, push and anything sent outward carry the kind, the id, the outcome, the time and the hash, never a parameter or a text.
- The gate is only as strong as the gateway's key grants: agents must not hold a key that can call the send tool directly.
  Package 2 adds a check for this.

## Work packages

| # | Package | Bin | Depends on | Detailed here |
|---|---------|-----|------------|---------------|
| 0 | Draw the line: `gate` package, provenance file, import rule | gate | none | yes |
| 1 | Actions of any kind: plugin `actions`, `approval_request`, `approval_list_actions` | gate | 0 | yes |
| 2 | Gateway connector from a configuration file; first user is a marketplace reply | connector | 1 | yes |
| 2c | Versions of a request: the owner's edit and a changed target become new versions instead of a discard | gate | 2, 3 | yes |
| 2d | The owner edits every field before approving: subject, To, Cc, Bcc, and every field an action type marks editable | gate | 2c | yes (gateway actions; mail with 6) |
| 3 | SQLite store with states, lasting keys and the hash in the log | gate | 0 | yes |
| 4 | iMessage on `ActionType` and `Executor` | connector | 1, 3 | yes |
| 5 | Calendar invitations on the same | connector | 1, 3 | yes (5a, 5b) |
| 6 | Mail on the same | connector | 1, 3 | later |
| 7 | `plugin_api` version 1; Beeper's settings leave the core configuration | gate | 1 | later |
| 8 | Signed checkpoints of the log, export | gate | 3 | later |
| 9 | Webhook as a way in and as a notice of status changes | gate | 3 | later |
| 10 | Standing approval: the owner names people a type may reach without waiting, through a request the owner approves | gate | 3, 5b, 6 | yes |

```
0 Line ─┬─> 1 Actions ─┬─> 2 Gateway connector
        │              ├─> 7 plugin_api
        └─> 3 Store ───┼─> 4 iMessage ─> 5 Calendar ─> 6 Mail
                       ├─> 8 Log checkpoints
                       ├─> 9 Webhook
                       └─> 10 Standing approval (after 5b and 6)
```

Package 0 moves files and changes no behaviour, so it is the cheapest point to agree on the line with upstream.
Package 1 comes before 2 because the connector needs a way to register a type that is not a chat message. Package 3 can run
beside 1 and must land before 9: a webhook that is delivered twice needs keys that outlast the decision (gap 8). Mail moves last
because it is the oldest path, has the most users and gains the least. Packages 4 to 6 remove the branches of gap 4 one kind at a
time; none of them is needed for a new service.

## Package 0: draw the line

This package only moves files; `git log --follow -M -C` still finds each file's history afterwards.

- New package `src/icloud_mcp/gate/` with `actions.py`, `external.py`, `outbox.py`, `status.py` (from `approval_status.py`),
  `withdraw.py` (from `approval_withdraw.py`), `audit.py`, `push.py` and `source.py`, which holds the `ApprovalSource` base
  class that was in `dashboard.py:159-201`.
- `textedit.py` stays outside: it imports `mail` for the mail source's text. It is `mixed` until package 6 moves that part.
  The gate still imports it (`TextLocked`, `TextInvalid`, `clean`), and through it `mail`; it also imports `callctx`
  (`gate/outbox.py:22`, `gate/withdraw.py:36`). Both ties keep the gate inside this server until package 6 removes them.
- The old module paths stay as aliases (`sys.modules[__name__]` is the new module), as `icloud_mcp.beeper` does today, so
  imports and monkeypatches written against them reach the module that runs.
- `dev/provenance.toml` maps path patterns to the bins `upstream`, `mergeable`, `gate`, `connector`, `mixed` and `fork`.
  `.gitignore` allows `gate/` and the file, because the repository tracks only what it lists.

Acceptance criteria:

1. Every file `git ls-files` lists under `src/`, `mac-helper/`, `menubar/` and `dev/` matches exactly one pattern of
   `dev/provenance.toml`; a test fails for a file with none or with two.
2. No module under `src/icloud_mcp/gate/` imports `icloud_mcp.mail`, `cal`, `contacts`, `imessage`, `messages`, `bridge`,
   `server`, `config`, `settings`, `dashboard`, `dashboard_api`, `approvals` or `plugins`, at the top or inside a function; a
   test walks the imports with `ast` and fails on one, and a second test proves the walk sees such an import.
3. `pytest tests --ignore=tests/integration -q` passes with no test file changed except for paths.
4. `tests/test_hub_golden.py` and `tests/fixtures/legacy_approvals_golden.json` pass unchanged.
5. Every file in the `mixed` bin is named in the table above, with what it mixes.

| Layer | What | Count |
|-------|------|-------|
| Unit | Provenance: every file has one bin; every entry names a file | +2 |
| Unit | Import rule for `gate`, and that the rule can fail | +2 |
| Unit | Each old module path is the new module | +7 |

## Package 1: actions of any kind

A plugin lists what it can do after approval, apart from whether it has chats.

```python
@dataclass(frozen=True)
class ActionSpec:
    action_type: ActionType
    executor: Callable[[PluginContext], Executor]
    allowed: Callable[[PluginContext], Iterable[str]]             # the owner's list; empty allows nobody
    policy: Callable[[PluginContext], Policy | None] = lambda c: None
    prepare: Callable[[PluginContext], Prepare | None] = lambda c: None

Prepare = Callable[[Mapping[str, Any]], Mapping[str, Any]]        # the agent's params -> the params that are stored
```

- `Plugin.actions: tuple[ActionSpec, ...]`. The server registers these next to the ones a `messages` capability brings, and
  `server.py:3011-3017` reads both.
- `prepare` is what `BeeperSender.send_request` does by hand today (`plugins/beeper/gateway.py:513-530`): it asks the service
  what the target is and stores that with the request, so the card shows the service's words and not the agent's. Fields that
  `prepare` adds are marked `readOnly` in the type's schema, and a request in which the agent sets one is refused.
- Two tools for agents. Two, and not one per type, because the tool descriptions have a character budget
  (`tests/test_tool_surface.py`) and a tool per type would grow with every service.

```
approval_list_actions() -> {actions: [{action, title, params_schema, text_field, allowed: "list" | "anyone" | "nobody"}]}

approval_request(action: str, params: object, request_id: str | None)
  -> {status: "queued_for_owner_approval", sent: false, outbox_id, approve_at, expires_in_seconds, already_queued?: true}
```

- `approval_list_actions` returns no entry of the owner's lists, only whether a list is empty, open or set.
- `approval_request` refuses an action that a channel tool owns (`beeper.message.send` stays with `messages_send_request`), so
  there is one way to ask for each thing.
- The seams of gap 4 that concern external requests close here: the withdrawal's log line takes its title from the action type,
  and the "not sure it arrived" notice no longer speaks of a conversation.

Acceptance criteria:

1. A made-up plugin with one `ActionSpec` and no `messages` capability, loaded through the entry point, gets a card, an edit,
   an approval and a status with no change to a file under `src/icloud_mcp/` outside the test's own plugin.
2. `approval_request` with params that break the schema answers with the path of the first error and queues nothing.
3. `approval_request` with a recipient that is not on the owner's list queues nothing, and the answer names no entry of the list.
4. A request whose params set a `readOnly` field is refused.
5. The same `request_id` with the same params returns the first `outbox_id` and `already_queued: true`; with other params it is
   refused.
6. Both names are in `NAMED_AFTER_WHAT_THEY_ANSWER` in `tests/test_tool_surface.py`, and the character budget still holds.
7. Without the dashboard neither tool is registered, and the instructions name neither.
8. Each test in the plan states its card: `protects=<what it protects>; fails_when=<the wrong result it rejects>;
   no_claim=<what green does not prove>`.

| Layer | What | Count |
|-------|------|-------|
| Unit | Registration from `Plugin.actions`; `prepare` and `readOnly` | +4 |
| Unit | `approval_list_actions` leaks no list entry | +1 |
| Integration | Made-up plugin: queue, edit, approve, status, withdraw | +5 |
| Integration | Tool surface, instructions and budget | +2 |

## Package 2: gateway connector

A service behind an MCP gateway joins through a file, with no Python. Whoever runs the server writes the file; the owner keeps
the lists on the dashboard.

- `GATE_ACTIONS_FILE` names the file. `GATE_GATEWAY_URL` and `GATE_EXEC_KEY` are in the environment only and are masked in every
  error.
- The client is `Gateway` from `plugins/beeper/gateway.py:89-128`, moved to `src/icloud_mcp/gate_connectors/gateway.py`; Beeper
  imports it from there. Its rule stays: `failed` only when the call provably never reached the tool.

```json
{
  "version": 1,
  "actions": [
    {
      "id": "market.message.send",
      "title": "Marketplace reply",
      "badge": "Market",
      "approve_label": "Send",
      "tool": "market-call_write_operation",
      "arguments": {"site": "example-market", "op": "sendMessage", "args": "$params", "idempotencyKey": "$request_id"},
      "params_schema": {
        "type": "object",
        "required": ["conversation_id", "text"],
        "additionalProperties": false,
        "properties": {
          "conversation_id": {"type": "string", "maxLength": 200},
          "text": {"type": "string", "minLength": 1, "maxLength": 2000},
          "conversation_title": {"type": "string", "readOnly": true}
        }
      },
      "text_field": "text",
      "recipient_fields": ["conversation_id"],
      "rows": [["Listing", "conversation_title"]],
      "describe": {
        "tool": "market-call_operation",
        "arguments": {"site": "example-market", "op": "getConversation", "args": {"id": "$params.conversation_id"}},
        "copy": {"conversation_title": "data.title"},
        "recheck": ["conversation_title"]
      },
      "result": {"path": "write", "sent": ["accepted", "applied"], "failed": ["not_sent", "not_applied"]},
      "allow_setting": "MARKET_SEND_ALLOWLIST"
    }
  ]
}
```

- `arguments` is what the tool is called with. `$params` is the approved params without the `readOnly` fields, `$params.<name>`
  one of them and `$request_id` the request's id, so a gateway tool that takes an idempotency key gets the same one each time.
- `describe` is the file's form of `prepare`: called when the request is queued, it copies fields of the answer into the params.
  The executor calls it again before it sends; when a field named in `recheck` differs, nothing is sent and the answer is
  `failed` with "it is not the one you looked at".
- `result` reads the tool's answer. A value in `sent` means sent and a value in `failed` means failed. Anything else is
  `unclear`: a missing path, an unknown value, an answer marked as an error, a timeout after the call left. Without `result`, an
  answer that is not an error means sent.
- `allow_setting` is the owner's list as a setting on the dashboard, empty by default, so a new action reaches nobody until the
  owner adds someone.
- A file the server cannot read, or one that fails its own schema, stops the start with one line that names the action and the
  field. A half-read file never registers anything.
- `icloud_check_health` gains one line per action: the execution key can list the tool. A second, optional check takes
  `GATE_AGENT_KEY_PROBE`, a key with the grants agents have, and reports a problem when that key can list the send tool.

Acceptance criteria:

1. With the file above and a fake gateway in the test, `approval_request("market.message.send", …)` queues a request whose card
   shows the title the fake gateway gave, and an approval calls the tool once with exactly the arguments the file describes.
2. The fake gateway answering `may_have_applied` gives `unclear`; `not_sent` gives `failed` and the request waits again;
   `applied` gives `sent`.
3. A connection that is refused before the session is up gives `failed`; a timeout after the call left gives `unclear` and no
   second call, counted at the fake gateway.
4. A title that changed between queue and approval sends nothing.
5. An empty `MARKET_SEND_ALLOWLIST` refuses every request; `*` allows every conversation.
6. A file with an unknown key, a row that names a field outside the schema, or two actions with one id stops the start.
7. Neither key appears in any error, log line or tool answer; a test searches all three for the key's value.
8. All 577 lines of `tests/test_beeper.py` pass with only the import of `Gateway` changed.

| Layer | What | Count |
|-------|------|-------|
| Unit | File schema, substitutions, result mapping | +8 |
| Integration | Fake gateway: queue, describe, approve, each outcome | +6 |
| Integration | Health lines, key probe | +2 |

### As built (2b)

- Agent-key probe (`gate_connectors/probe.py`): one `list_tools` per key in `GATE_AGENT_KEY_PROBE`, in the background at start
  and on every `icloud_check_health` call. Each action is `ok`, `bypassable` or `unchecked` (no key set, no run yet, or a key
  that could not list). Cards read the last result.
- Holding approval is a gate feature, not a gateway one: `ActionSpec.hold(ctx)` returns an `actions.Hold`, level `blocked` (the
  card shows why, the approve button is off, the dashboard and `release` refuse with nothing claimed) or `note` (one line on the
  card, never blocks). A hold that raises blocks. `approval_request` still queues a held request and answers
  `warnings: ["bypassable"]` with a `warning` sentence (X10); `bypassable` is no refusal code.
- Health: the area `gateway` lists one line per action (`problem`, `warning`, `ok`); the Server page puts problems first, then
  warnings, and folds two or more fine lines into one that opens on a tap (H2).
- The owner's lists: each `allow_setting` is a field of the connector's settings page `gateway`, one section per action
  (S5). An empty list shows "Reaches nobody yet" with a link that focuses the field (E2, J1). Adding an entry or `*` without a
  recent sign-in asks to confirm, and the confirm sheet names the action (`why`); removing one does not. The service page links
  the lists ("Who it may reach").
- Owner decisions after the review (2026-11-15): `GATE_PROBE_REQUIRED=true` makes "not checked" block; an approval whose
  last probe run is older than 10 minutes (`MAX_AGE`) runs the probe first, so a grant added since cannot slip through; a run
  in which the bypassing key cannot list keeps the action bypassable; runs list the keys at once and a caller that waited for a
  run takes its result (10 s). The owner's lists apply after a restart, like every dashboard setting, and the page says so.
- Deviation from E1: the card's "not checked" line is a `hold` note, not the type's `warnings`. A warning turns the send into
  "Send anyway…", and a note that never blocks must not.
- S-a is replaced by package 2c: until then a changed target still ends the request as discarded (`target_changed`).
- `dev/preview_dashboard.py --gateway bypass|clean|unchecked` shows all of it with made-up data.

## Package 2c: versions of a request

The owner asked for this after the 2b mock: a request is never thrown away because something changed; it gets a new version,
and the old ones stay readable. Accepted layout: the current version on top, the versions as a list below it (mock variant A).

- A request has versions 1..n. Each is immutable, has its own canonical bytes and sha256, and says who made it: the assistant
  (v1), the owner's edit ("Edit text" makes a new version instead of overwriting; replaces "Bring it back"), a changed target
  (the recheck before the send found new values: nothing is sent, the request waits again as a new version with the change
  marked), or the assistant revising it (`approval_request` with `replaces: <outbox_id>`).
- Approving needs the sha of the newest version; only that one can be sent. Older versions are read-only on the card.
- Changes D7 and S1: `target_changed` no longer ends a request and no longer frees its key; the store gains a versions table
  (user_version 2, with a migration). Activity and the audit log name every version with its sha256.
- Tests: a recheck difference leaves the request waiting as version n+1 with nothing sent; approving with an older sha is
  refused; an edit makes a version and the assistant's version stays readable; `replaces` from another request's key is refused.

### As built (2c)

- Store version 2 (`gate/store.py`): table `version` (request, n, time, author `assistant` | `owner` | `service`, sha256, raw).
  `add` writes version 1, `replace` the next (the owner's edit, or the agent's revision with author `assistant`), and the new
  `revise` puts a request that was being carried out back to waiting as a version by the service. Ending a request drops the
  raw of every version and keeps author, time and hash; the hourly purge deletes versions of deleted requests. A version 1 file
  gets one version per request at the first start; a version 2 file is refused by an older server, as before (export first).
- Recheck contract (`Registration.recheck`): None (unchanged), the parameters with the service's new values (a new version
  waits; `actions.Revised`, dashboard answers 409 with `revised`, `version`, and the log gets `approval.revised` with both
  hashes), or a code (the request ends as before). The gateway connector returns new values; a key stays bound, so asking
  again with the same `request_id` returns the same request.
- `approval_request(..., replaces=<outbox_id>)`: a new version of a waiting request of the same caller, action and key, after
  every check a new request gets (the list, the policy, describe). Refusal code `not_replaceable`. The answer has `replaced`
  and `version`.
- Card (second mock, variant A, owner 2026-11-16, after the first build read as too heavy): the card as before 2c; a
  "Changed since you looked" box only when the service changed something, naming old and new value, with the new value marked;
  one row "Earlier versions" under the card that opens a "History" sheet, newest first, in sentences ("Market changed the
  listing", "You changed the text", "Your assistant asked"); an older one opens read-only on a tap, and the assistant's first
  text offers "Use this text again" when it was edited. The owner's edits in a row are one version, and going back to the text
  before them removes it. Buttons are not in capitals in the Material look either.
- Mail, calendar and iMessage keep their single edit until packages 4 to 6 move them to the store.

## Package 2d: edit every field

The owner asked for it on 2026-11-16: not only the text, also the subject, To and Cc of a mail and the agent-set fields of any
action. A changed recipient goes through the owner's list and the policy again before the new version is stored (and for a file
action through `describe`), so an edit cannot reach someone the list does not allow. Each edit is a version (2c). Mail moves to
the store with package 6, so its fields join versions there; until then the mail card keeps its text-only edit. Needs a mock first.

### As built (2d, gateway actions)

- Mock (owner 2026-11-17, Artifact „Sendgate Paket 2d“): variant A, „Edit“ opens one form like a mail being written, the label and
  the value on one line, the text below. Changed by the owner's comment on the mock: the list guards against the assistant, not
  the owner. A recipient the owner types in who is not on the list is allowed for that request only, with a note on the form; the
  list itself does not change. The type's other rules (`policy`) still apply, and the service is asked about the new target
  again (`prepare`), so the card shows the new listing.
- Connector file: `editable_fields` (see the reference below); `ActionType.editable_fields` and `recipient_fields` for plugins.
- The version's canonical bytes carry `owner_allowed` (sorted, left out when empty, so older entries keep their bytes and hash).
  Approving reads it from the version it approves: only those recipients skip the list. A recipient the assistant set is
  checked against the list as before, also after the owner edited something else. A recheck's new version keeps it.
- `PUT /dashboard/api/items/{key}/fields {"sha", "fields": {name: value}}`: one owner version for the whole save (edits in a row
  stay one version, as in 2c). Refused with nothing stored: a field that is not editable, a value the schema does not take, an
  older `sha`. The log line `approval.edited` names the labels of what changed (never the values) and how many recipients the
  owner allowed by hand.
- A request's key stands for its parameters without the editable fields, so the assistant asking again with the same key after
  the owner's edit finds the same request.
- A type without `editable_fields` keeps the text editor of 2c unchanged.

## Package 3: store

One SQLite file, `DATA_DIR/approvals.db` (mode 600, WAL), through the standard library's `sqlite3`. The `Outbox` class keeps its
methods (`add`, `pending`, `claim`, `replace`, `restore`), so the four sources do not change.

```sql
CREATE TABLE request (
  id           TEXT PRIMARY KEY,
  source       TEXT NOT NULL,              -- mail | calendar | imessage | external
  action_type  TEXT,
  state        TEXT NOT NULL CHECK (state IN
                 ('waiting','executing','sent','unclear','discarded','withdrawn','expired')),
  created_at   REAL NOT NULL,
  expires_at   REAL NOT NULL,
  decided_at   REAL,
  sha256       TEXT NOT NULL,
  key          TEXT,
  requested_by TEXT,
  raw          BLOB,                       -- these four are NULL once the request has ended
  recipients   TEXT,
  followup     TEXT,
  edited_from  TEXT
);
CREATE INDEX request_waiting ON request (source, state, created_at);
CREATE INDEX request_key ON request (source, key) WHERE key IS NOT NULL;
```

| From | To | When |
|------|----|------|
| `waiting` | `executing` | The owner approved; `claim(id, "execute")` |
| `executing` | `sent` | The executor answered `sent` |
| `executing` | `unclear` | The executor answered `unclear` or raised, or the server started and found the row in `executing` |
| `executing` | `waiting` | The executor answered `failed`; `restore` |
| `waiting` | `discarded`, `withdrawn`, `expired` | The owner, the agent that asked, or the clock |

- A request that has ended keeps its id, source, type, state, times, hash and key, and loses its content. That matches today:
  the text lives only in the entry and goes with it.
- Ended rows are deleted after `AUDIT_KEEP_DAYS`, the same span the log keeps.
- For the `external` source a key lasts as long as its row. Asking again with a key whose request has ended queues nothing and
  answers with that request's status record. Mail, calendar and iMessage keep today's rule (a key matches waiting requests
  only), because their key names a saved draft that may be sent again.
- On the first start the four JSON files are read into the table as `waiting` and renamed to `<name>.migrated`.
  `python -m icloud_mcp.gate.export_queue DATA_DIR` writes the waiting rows back in the old format and ends them in the store.
- Every log line of a request gains `sha256`: `tool.queued`, `approval.edited`, `approval.edit_undone`, `approval.sent`,
  `approval.failed`, `approval.discarded`, `approval.withdrawn`, `approval.expired`.

Acceptance criteria:

1. A process killed between `claim` and the executor's answer leaves the row in `executing`; the next start sets it to `unclear`,
   writes one `approval.sent` line with delivery `unconfirmed`, and `approval_status` answers `unclear`.
2. Two threads that approve one request at the same moment produce one executor call.
3. `approval_request` with the `request_id` of a request that was sent 1 hour ago queues nothing and returns status `sent`.
4. After a request ended, `SELECT raw, recipients, followup, edited_from` for its row returns four NULLs.
5. Starting on a `DATA_DIR` with the four JSON files of `tests/fixtures/legacy_approvals_golden.json` gives the same waiting
   requests, with the same ids and hashes, and four `.migrated` files.
6. `python -m icloud_mcp.gate.export_queue`, run after that start, writes files that the previous version loads with the same ids and hashes.
7. For one request that was edited once and then approved, the log holds three lines whose `sha256` values are the first hash,
   the second hash and the second hash again.
8. The 10 test files that name a queue file read through the store and pass.

| Layer | What | Count |
|-------|------|-------|
| Unit | Each transition, and each one that must be refused | +9 |
| Unit | Key rule per source | +3 |
| Integration | Crash recovery, two approvals at once | +2 |
| Integration | Migration from the golden files and back | +2 |

### As built

Package 3 is built for the external source only, as decision D5 says: `gate/store.py` holds plugin actions and Beeper
messages; mail, calendar and iMessage keep their JSON files until packages 4 to 6, and criteria 5, 6 and 8 apply to
`external-outbox.json` only until then. The table has three columns more than the sketch above, from the review obligations:
`attempts` (E-4), `outcome_reason` (E-15) and `key_fingerprint` (E-10), and the key index is unique for the external source
(E-11). `claim(id, state)` takes `executing`, `discarded` or `withdrawn`; `finish(id, state, reason)` ends a request that was
being carried out. A row found `executing` at start ends `unclear` with reason `interrupted` and is logged once as an
`approval.sent` with delivery `unconfirmed`; a `reported` column makes each such report and each expiry happen exactly once, also across a restart. Only the first store a process opens on a DATA_DIR recovers rows. A corrupt queue file is set aside and the start continues (E-1 replaces the "stops the start" of X15). `PRAGMA user_version` is 1; a newer database stops the start. The export lives in the package (`python -m icloud_mcp.gate.export_queue`), so it runs in the Docker image. `Store(data_dir, source, ...)` takes the source; the JSON `Outbox` keeps its
`filename` until packages 4 to 6 move its sources. Health reports a queue file that could not be read as `approval_store`.

## Package 4: iMessage on ActionType and Executor

iMessage requests move from `imessage-outbox.json` to the store, and later onto the external source as action type
`imessage.send`. Mapped on 2026-11-17 against `imessage.py`, `dashboard.py:313-350` and `gate/external.py`. The owner split it
the same day, once the code showed the second half changes the card: 4a moves the queue and fixes the lost request without a
visible change; 4b folds iMessage into the external source and needs a mock first.

### 4a: the store

| Today | After 4a |
|-------|----------|
| `imessage-outbox.json`, rewritten on every change, no state for "being sent" | Rows of source `imessage` in `approvals.db`, with the states of package 3; the file is read in once and renamed `.migrated` with ids and hashes kept |
| A refusal or bridge error after the claim loses the request while the card says "still waiting" | Each ending is a state: see the table below |
| Edit overwrites once (`edited_from`) | Edit makes a version (2c), so the card gets "Earlier versions" like a gateway action |
| A crash between approve and the Mac's answer loses the request | It ends `unclear` at the next start and is logged once, also when another source's store opened first |
| Members added to a group since queuing are sent to when the lists allow them | Nothing is sent: the request waits again as a version by the service with the members as they are now |

| What happens after the owner approved | The request |
|---------------------------------------|-------------|
| Reading the conversation fails (Mac offline, any bridge error) | waits again; the owner sees why |
| The conversation is gone | ends `discarded`, reason `target_changed` |
| A member is no longer allowed (never-send, list) | ends `discarded`, reason `not_allowed` |
| The members changed and all are allowed | waits again as a new version (2c) |
| The Mac says nothing was sent, or never got it | waits again (`failed`) |
| Any other bridge error | ends `unclear`, never repeated |
| The Mac answers `sent` | ends `sent` |
| The Mac answers `failed` or `unconfirmed` | ends `unclear` (it may have gone out) |

Unchanged: the stored bytes and their hash, the tool's answer, the card, `approve_at`, the `/outbox` page, the log lines.
`python -m icloud_mcp.gate.export_queue` writes `imessage-outbox.json` back as well.

Acceptance criteria:

1. The iMessage tests (`test_messages_send.py`, `test_imessage_send.py`, `test_imessage_sms_only.py`, `test_text_edit.py`) pass with
   the store; a changed test is reported on its own line with its reason (rule 3).
2. Each row of the table above has a test that checks the state and that nothing was sent twice.
3. Starting on a `DATA_DIR` with an `imessage-outbox.json` gives the same waiting ids and hashes and a `.migrated` file; the export
   writes a file the previous version loads with the same ids and hashes.
4. A row of source `imessage` left `executing` ends `unclear` at start even when the external store opened first.

### 4b: one source

iMessage becomes action type `imessage.send` on the external source, and the branches of gap 4 for it go: `withdraw.py`,
`push.py`, `dashboard_api.py`, `textedit.py`, `dashboard.py`, `approvals.py`, `server.py`, `today.js`. The owner chose the card
on 2026-11-17 from a mock with three variants (variant B): the title is the type's own, the person or chat moves to the line
below it, and the Group tag, the bubble and the second confirmation stay.

| On the screen | Today | After 4b |
|---------------|-------|----------|
| List, first line | iMessage to Weekend hike | iMessage |
| List, second line | iMessage · Group | iMessage · Group · to Weekend hike |
| Card title and line under it | iMessage to Weekend hike / iMessage · asked by … | iMessage / to Weekend hike · asked by … |
| Group warning, bubble, Edit text | as now | unchanged |
| Buttons for a group | Discard (main), Send anyway… | unchanged |
| Note under the card | Sent from your Mac once you approve. | unchanged |
| Sidebar "Waiting by kind" | iMessage | unchanged (the type's badge) |
| `/outbox` page | lists mail and iMessage | lists mail and every waiting action, iMessage among them |

What the action type gains. All three fields are optional, so the gateway types and plugin types keep working as they are and
may use them later:

| Field | What it does | `imessage.send` sets |
|-------|--------------|----------------------|
| `tags(params)` | short labels after the badge in the list and on the card | `["Group"]` for a group chat |
| `text_style` | `"plain"` (default) or `"bubble"` for the text block | `"bubble"` |
| `confirm_twice(params)` | a reason; when set, the card shows Discard as the main button and "Send anyway…", which asks once more with this reason | "Everyone in Weekend hike receives it." for a group |

The type also sets `note` ("Sent from your Mac once you approve.") in place of the general note of external actions.

How the work maps onto the external source, which already has every ending 4a introduced:

| 4a ending | In 4b |
|-----------|-------|
| Reading the conversation fails | the type's `recheck` raises: the request waits again (`external.py`, recheck) |
| The conversation is gone; hidden since queuing; someone no longer allowed | `recheck` answers `target_changed` or `not_allowed`: ends `discarded` |
| The members changed, all allowed | `recheck` answers the new parameters: a new version by the service |
| The Mac says nothing was sent, or never got it | the executor answers `failed`: waits again |
| Any other error; Mac `failed` or `unconfirmed` | the executor answers `unclear` |
| Mac `sent` | `sent` |

Start-up moves every row of source `imessage` to source `external` once, inside one transaction: same id, state, versions and
expiry, the parameters in the canonical form of the type, a new hash (D4-1) and one log line `approval.migrated` with both
hashes. An `approve_at` link or push notice sent before the update names `imessage:<id>`; the dashboard and `/outbox` open
`external:<id>` for it. `python -m icloud_mcp.gate.export_queue` writes
waiting `imessage.send` rows back to `imessage-outbox.json` in the old form, so 4a can read them; their hash changes again then.

Unchanged for the agent: `messages_send_request` answers as now (`queued_for_owner_approval`, `outbox_id`, `to`), and
`approval_status` and `approval_withdraw` answer for an id from before the update.

D4-2, open: the activity log line names the title only (`dashboard_api.py:493`), so after 4b it reads "Approved imessage
“iMessage”" without the person or chat. Recommended: the log entry gets a `to` field from the type's `recipients`, only for types
the server registers itself (iMessage now, calendar and mail in 5 and 6); a type from a connector file or a plugin keeps logging
the title only, because its recipients come from the agent's parameters (`actions.RESERVED_LABELS`). Activity shows the `to` field
as "to Weekend hike". Without it, who got a message is visible only in the request's own history.

Acceptance criteria:

1. Each row of the screen table has a test against the JSON the dashboard reads (`/dashboard/api/...`), and the QA run checks it
   on desktop and at 375 px, Send anyway included.
2. The 4a tests in `test_imessage_release.py` pass against the external source with the same states and the same "sent once"
   checks; a test that had to change is reported on its own line with its reason (rule 3).
3. Starting on a `DATA_DIR` with waiting and ended iMessage rows of source `imessage` gives the same ids, states and versions
   under `external`, one `approval.migrated` line per waiting row, and no row of source `imessage`; a second start changes nothing.
4. After 4b, a search of `src/` for `kind == "imessage"` finds nothing outside `imessage.py` and the migration.
5. Rollback: the export, then 4a, shows the same waiting requests.

## Package 5: calendar invitations on ActionType and Executor

Calendar changes that make iCloud email other people (`ALLOW_CALENDAR_INVITES=approve`) move from `calendar-outbox.json` to the
store, and later onto the external source. This was checked against `cal.py:1640-1733`, `dashboard.py:247-310` and
`server.py:1373` on 2026-11-18. The proposed split follows package 4 (D5-1). 5a moves the queue and fixes what goes wrong after
approval, and the owner sees nothing new. 5b folds the calendar into the external source and changes the card, so it needs a
mock first.

What the calendar does today, and where that breaks a rule of the gate:

| # | Today | Rule it breaks | Evidence |
|---|-------|----------------|----------|
| C1 | Any error after the claim puts the request back (`restore`), including a timeout after iCloud already wrote the event and emailed the guests | `failed` only when the other side provably never got it | `dashboard.py:288-296` |
| C2 | A 412 (the event changed since the agent asked) puts the request back, and every later approval fails the same way | An ending that cannot change ends the request | `cal.py:1726-1728` |
| C3 | `INVITE_ALLOWLIST` and `MAX_ATTENDEES` are checked when the agent asks, never at approval | The owner's lists are checked at queue and at approval | `cal.py:1660`, `cal.py:1710` |
| C4 | A crash between the claim and iCloud's answer loses the request | Gap 7 | `outbox.py` |
| C5 | Deleting an event that is already gone counts as done, although nobody was emailed | The log says what happened | `cal.py:1729` |
| C6 | An edited description overwrites once (`edited_from`) | Versions (2c) | `dashboard.py:308-310` |

### 5a: the store

| Today | After 5a |
|-------|----------|
| `calendar-outbox.json`, no state for "being written" | Rows of source `calendar` in `approvals.db`. The file is read in once and renamed `.migrated`, keeping ids and hashes |
| Any error puts the request back | Each ending is a state (table below) |
| Lists checked only when asked | Checked again at approval, before anything is written |
| Edit overwrites once | Edit makes a version (2c) |
| A crash after approval loses the request | It ends `unclear` at the next start and is logged once |

| What happens after the owner approved | The request |
|---------------------------------------|-------------|
| A guest (for a reply: the organizer) is no longer allowed by `INVITE_ALLOWLIST`, or there are more than `MAX_ATTENDEES` | ends `discarded`, reason `not_allowed`; nothing is written |
| The event changed (another ETag) or is gone, read before the write | ends `discarded`, reason `target_changed` |
| iCloud cannot be reached before the write goes out: refused connection, name lookup, connect timeout, sign-in refused | waits again (`failed`); the owner sees why |
| iCloud answers 412 or 404 to the write itself: the event changed or went in the seconds since the read | waits again (`failed`), and the next approval ends it `target_changed` at the read (D5-6) |
| iCloud answers another 4xx | waits again (`failed`): a 4xx means nothing was written |
| iCloud answers 5xx, a read timeout, or the connection drops after the write went out | ends `unclear`, never repeated (D5-2) |
| iCloud answers 2xx | ends `sent` |

Which network errors count as "before the write goes out" is a closed list. Of the errors that `niquests` raises (the HTTP
library caldav uses, with the names of `requests`), only `ConnectTimeout` and a `ConnectionError` caused by
`NewConnectionError` count (`NameResolutionError` is one; a refused connection is one), and a refused sign-in. Every other
transport error ends `unclear`, and so does caldav's `RateLimitError`, because it reports a 429 and a 503 with Retry-After alike. A delete of an event that is already gone ends at the read, with `target_changed` (D5-3).

The lists are the calendar's own, as when the agent asks: an empty `INVITE_ALLOWLIST` allows everyone, a domain entry allows
the domain (`mail.recipient_allowed`), the owner's own addresses do not count, and a reply checks the organizer
(`cal.py:2070`). The check at approval calls the same function as the check at queue time and has no copy of it.

`sent` means iCloud saved the change. Whether iCloud's emails then reached the guests is not known to this server, and the
success line says so: "Added “Autumn fair”. iCloud now emails jana@example.org." (`cal.py:1732`) becomes "Added “Autumn
fair”. iCloud was asked to email jana@example.org." (`cal.py:722-750` already tells a saved event apart from a delivered one). Nothing is written again to repair
a delivery.

5a also writes the request's later type into the row's `action_type` column (`calendar.invite`, `calendar.update`, … in 5b),
because the operation lives in the request's content, and an ended row has no content left (package 3). The description is
kept in full: the copy in the metadata is cut at 4000 characters (`cal.py:1695`), and the card and the write take it from the
stored object.

Unchanged: the stored bytes and their hash, the tools' answers (`queued_for_owner_approval`, `outbox_id`, `approve_at`,
`would_email`), the card, the log lines. `python -m icloud_mcp.gate.export_queue` writes `calendar-outbox.json` back as well.
Without the dashboard, a change that would email someone stays refused, as today (`server.py:1373`).

Acceptance criteria:

1. The calendar approval tests (`test_dashboard.py`, `test_dashboard_cards.py`, `test_text_edit.py`, `test_approval_withdraw.py`,
   `test_dashboard_links.py`) pass with the store. A changed test is reported on its own line with its reason (rule 3).
2. Each row of the endings table has a test against a made-up CalDAV server. The test checks the state, the reason, and how many
   PUT or DELETE calls were made: one at most per approval, none after `not_allowed`. The server honours `If-Match` and
   `If-None-Match` (the fake in `tests/caldav_fakes.py` ignores them today), so the 412 row changes the event between the read
   and the write and checks that the server's copy stayed the other writer's.
5. A description longer than 4000 characters is written in full when the owner approves without editing.
6. An empty `INVITE_ALLOWLIST`, a domain entry and a reply to an organizer who is not on the list behave at approval as they do
   when the agent asks.
3. Starting on a `DATA_DIR` with a `calendar-outbox.json` gives the same waiting ids and hashes and a `.migrated` file. The export
   writes a file the previous version loads with the same ids and hashes.
4. A row of source `calendar` left `executing` ends `unclear` at start, also when another source's store opened first.

### As built (5a)

`CalendarService.approve(outbox, item_id, sha256)` in `cal.py` replaces `release()` and ends every row as the table says; the
dashboard's calendar source only maps its answer. `Store(type_of=...)` types the rows read in from the file (`cal.calendar_type_of`).
The read before the write is a GET of the event, which also proves the connection works before anything is written. A sent change
logs as before (no `delivery`), an unclear one with delivery `unconfirmed`. Tests: `tests/test_calendar_release.py` (every row of
the table, the list rules, a reply, the full description, the start-up), two tests in `tests/test_store.py`; `caldav_fakes.Server`
honours `If-Match` and `If-None-Match`.

### 5b: one source

Calendar changes become actions on the external source, and the calendar branches of gap 4 go: `withdraw.py:46-58`,
`dashboard_api.py:279`, `:292`, `today.js:29`, `:239-240`, `app.js:10`, `edit.js:7`, `dashboard.py` (`CalendarApprovals`) and
`server.py` (`approval["calendar"]`).

One type per operation (D5-4), so the badge and the main button stay fixed per type as `ActionType` requires:

| Type | Badge | Main button | Who it reaches |
|------|-------|-------------|----------------|
| `calendar.invite` | Invitation | Add and invite | the guests |
| `calendar.update` | Event update | Save and notify | the guests |
| `calendar.delete` | Cancellation | Delete and notify | the guests |
| `calendar.cancel` | Cancellation | Cancel and notify | the guests |
| `calendar.reply` | Reply | Send answer | the organizer |

Parameters: `calendar_url`, `calendar`, `uid`, `href`, `etag`, `summary`, `start`, `end`, `location`, `guests` or `organizer`,
and `ics`, the whole object as it will be written. `ics` is the only source of the description: the text field shows the
description of its one VEVENT, and an edit rewrites that VEVENT inside `ics`, so the stored bytes are what is written. The
locks of today stay: only `calendar.invite` and `calendar.update` have a text field, and an object with more than one VEVENT
(a recurring event with an overridden occurrence) cannot be edited (`textedit.py:77-85`). If the external source cannot lock
the text for one request while its type allows it, 5b adds that.

Registration follows `imessage.py:442`: `allowed` is `["*"]`, because the calendar's lists differ from the gate's exact
matching (an empty list allows everyone, a domain entry allows the domain); the type's `policy` and `recheck` call the
calendar's own check, and `recheck` reads the ETag, which gives the endings at the read. `builtin=True`, and `channel_tool`
names the calendar tools, so `approval_request` refuses the calendar types. The executor gives the other endings.

Unchanged for the agent: the calendar tools answer as now (`queued_for_owner_approval`, `outbox_id`, `approve_at`,
`would_email`), and `approval_status` and `approval_withdraw` answer for an id from before the update.

What the card loses unless the type gains something. All of it is decided on the mock:

| On the screen | Today | 5b without new fields |
|---------------|-------|-----------------------|
| Card and list title | the event's summary ("Autumn fair") | the type's title ("Invitation"); `ActionType.title` is never taken from a parameter |
| List, second line | Invitation · to 3 people | Invitation · to jana@example.org, … |
| Text block | hidden when the description is empty, caption "The description exactly as it will be saved." | an empty block, caption "Exactly as it will be sent." |
| Notes | the effect per operation ("Approving deletes the event, and iCloud emails a cancellation to 3 people.") | the type's `note`, the same for every request of that type |

The mock shows three variants. A: the type's title, with the summary as the first row ("Event: Autumn fair"). B: a new optional
field `title_field`, only for types the server registers itself (`builtin`). The summary is the agent's text in either case,
as on today's card; B keeps that exception to the server's own types, so a connector file cannot make a parameter the title. C: like A, with the summary also on the list's second line. The text block is hidden when
the text is empty for every type, and a new optional field `text_caption` keeps the calendar's caption. A card with a
text and no caption of its own looks as before.

Start-up moves every row of source `calendar` to source `external` once, as in 4b: same id, state, versions and expiry, a new hash,
one `approval.migrated` line per row. `#/item/calendar:<id>` opens `external:<id>`. `Store.adopt` takes one type and converts the
raw bytes alone (`gate/store.py:499`), but a calendar request is the object plus its metadata and has one of five types, so
`adopt` gains a conversion that sees the whole row and reads the type from the `action_type` column 5a wrote. The export needs
the inverse, from the parameters back to the object and its metadata in the 5a form, because the export of 4b only writes JSON.

Acceptance criteria:

1. Each row of the screen table, in the variant the owner chose, has a test against the JSON the dashboard reads. The QA run
   (gstack `/qa`) checks every type on desktop and at 375 px, including a description edit.
2. The 5a ending tests pass against the external source with the same states and call counts.
3. Starting on a `DATA_DIR` with waiting and ended calendar rows gives the same ids, states and versions under `external`, with
   one `approval.migrated` line per waiting row; a second start changes nothing.
4. After 5b, a search of `src/` for `kind == "calendar"` or `"calendar"` as a source finds nothing outside `cal.py` and the
   migration.
5. Rollback: the export, then 5a, shows the same waiting requests, and approving one of them writes the event.

### Decisions for package 5

| # | Question | Recommended | Why |
|---|----------|-------------|-----|
| D5-1 | One package or 5a and 5b? | 5a, then 5b | 5a fixes C1 to C4 without a visible change; only 5b needs a mock |
| D5-2 | What if the connection drops after the write was sent? | `unclear`, no read-back | iCloud rewrites the stored object, so comparing it with what was sent is not reliable; a repeat would email the guests twice |
| D5-3 | Deleting an event that is already gone | `discarded`, `target_changed` | Today it says "Deleted … iCloud now emails …", which did not happen |
| D5-4 | One type, or one per operation? | One per operation | Badge and button stay fixed per type, and the owner's list of types names what each one does |
| D5-5 | The card title | open, decided on the mock | Variants A to C above |
| D5-6 | A 412 or 404 to the write itself, after the read found the event unchanged | `failed`, then `target_changed` at the next read | It is true that nothing was written, and the gate keeps its three answers. The other way, a fourth executor answer that ends a request, changes the gate for a race of seconds |

Reviewed on 2026-11-18 with gstack `/plan-eng-review` and a second opinion from Codex. Codex raised five points, all taken in
above: the 412 or 404 at the write (D5-6), the calendar's own list rules, the full description, the migration by row, and what
`sent` means for the guests.

Effort, estimated and not measured: 5a about 240 min of work and 30 min of review; 5b about 300 min of work after the mock,
plus 45 min of review.

## Package 10: standing approval for named people

Owners say things like "invitations to my partner don't need my approval". Today the server cannot express that. `INVITE_ALLOWLIST`,
`SEND_ALLOWLIST` and the type lists say who may be reached at all; whether a request waits is one switch for everyone
(`ALLOW_CALENDAR_INVITES`, `SEND_REQUIRES_APPROVAL`). An agent that hears the wish can only tell the owner to loosen the switch,
and that loosens it for every guest (seen in the live system on 2026-11-18).

Package 10 adds a second list per action type: who may be reached **without** waiting. The agent may ask for an entry, and the
entry only exists once the owner approves it, so a rule change is itself a request on the gate. The rule "the owner approves"
still holds: for these requests the owner approved in advance, with a fresh sign-in, for one person and a limited time.

### What it adds

| Part | What it is |
|------|------------|
| Standing rule | One action type, one recipient, an end date. Stored in a new table `standing` in `approvals.db`: id, type, recipient (case-folded), created, expires, the id of the request that created it, revoked |
| Type `gate.trust` | A request for a rule, registered by the server itself (`builtin`). Parameters `action_type`, `recipient`, `days`, `reason`. Asked for with `approval_request`, so no new tool |
| Standing release | When a request is first queued, the server checks its rules. If every person the request reaches has a live rule for its type, the server releases it itself, through the same `release()` the owner's click uses |
| `gate.submit()` | One call for every tool that queues: it queues, tries the standing release, and answers either "waiting" or the release's outcome. The push for a new request goes out only when the answer is "waiting" |
| Settings list | "Without approval" in the dashboard: type, person, until when, how often used, Remove |

### The request for a rule

The card is built from the fields alone, never from the agent's words: title "Allow without approval", rows "Action"
(the type's title and badge, not its id), "Person", "Until" (a date). The reason is shown as a quote marked as the assistant's
words. `days` defaults to 90 and may not exceed 365.

`queue` refuses, with `policy`:

- an `action_type` that is not registered, is `gate.trust` itself, or has not opted in (below);
- a recipient the type's own list does not allow (a rule never widens who may be reached, it only skips the wait);
- a recipient that is a domain, a wildcard or empty: a rule names one person, matched exactly.

Approving needs a recent sign-in, as loosening a setting does today (`dashboard.py:509`, five minutes). The approve endpoint
has no such check per type yet, so `ActionType` gains `step_up: bool = False`, and the endpoint answers with the same
`confirm` 403 that settings use (`dashboard_api.py:52`) when the type asks for it and the sign-in is old. `gate.trust` registers
with `allowed` `["*"]`, because its `recipient` is checked by its policy against the target type's own list, not the gate's. The executor writes the
row locally and answers `sent`; a write that fails answers `failed`, so the request waits again. It never answers `unclear`.

A rule for the same type and recipient replaces the older one (new end date). Removing a rule takes effect at once and needs no
approval, because it only makes requests wait again. The owner removes rules in the dashboard; agents cannot.

### The standing release

Who a request reaches, for this purpose, comes from a new function of the type, `standing_identity(params)`: a list of stable
identities of single people (a lower-case email address), or `None` when the type cannot say (a group, a name only, an empty
list). `recipients()` is not enough: iMessage gives a display name there (`imessage.py:398`) and Beeper a chat id that may be a
group. `None` or an empty list always waits.

| Request | What happens |
|---------|--------------|
| `standing_identity` names one or more people, each has a live rule for the type, no hold | released by the server at once; the agent's answer carries the outcome |
| `standing_identity` answers `None` or `[]` | waits, whatever rules exist |
| The request already waited once and the owner has not decided it (asked again with the same bytes or key, or put back after `failed` or a new version) | waits: a standing release is tried once per request, when it is first queued |
| One recipient without a rule, an expired or removed rule | waits for the owner as today |
| The type's `confirm_twice` returns a reason (a group chat) | waits; a rule never answers a second question |
| The release answers `failed` (nothing reached the service) | waits for the owner, with the reason |
| The rule's daily limit is reached (default 20 uses a day) | waits, and the card says the limit was reached |

Checking the rules, counting the use and claiming the request happen in one SQLite transaction in `approvals.db`, so two
requests cannot both take the last use of a day, and a rule removed before that transaction no longer counts. A rule removed
after it does not stop the request already being carried out. Uses are rows of a table `standing_use` (rule id, request id,
time); the daily limit counts them over the last 24 hours.

The release runs every check the owner's click runs: the lists, the policy, the hold (an agent key that can call the send tool
directly still blocks), the recheck and the executor's three answers. `unclear` stays `unclear` and is never repeated.

What the agent sees: the queuing tool answers `sent` or `unclear` with the `outbox_id` and `without_approval: true`, instead of
`queued_for_owner_approval`. Today each tool builds its "queued, not sent" answer itself (for example
`plugins/beeper/gateway.py:456`), and `Store.add` fires the push before it returns (`gate/store.py:347`). Both move behind
`gate.submit()`, and every tool that queues uses it: the calendar tools, `messages_send_request` and `approval_request`. `approval_status` answers as for any request.

What the owner sees: Activity marks the line "without approval" and names the rule. The log line carries the rule id as the actor
(`standing:<id>`), never the recipient of a type that is not `builtin`. A push only goes out for requests that wait, as today.

### Which types may use it

A type opts in by having `standing_identity`. Package 10 gives it to the five calendar types (guests and organizer are email
addresses) and to mail (every address in To, Cc and Bcc; a mail to several people needs a rule for each). iMessage and Beeper get
it later, once they can name one person for a one-to-one chat and refuse groups; until then they always wait. A connector file
cannot opt in, because it has no way to say who a chat id is. `gate.trust` itself never opts in.

The calendar becomes an action type in 5b and mail in 6, so the order is 5b, 6, then 10. Mail keeps the rules it has today on top
(`SEND_ALLOWLIST`, `MAX_RECIPIENTS`): a rule only skips the wait. Whether a mail with an attachment may go without a look is
a question for the mock of package 6.

### Out of scope for package 10

- Domains, groups or "everyone in my contacts" as a rule. One rule, one person.
- Rules that depend on the content (only invitations on weekends). That would be a policy language.
- An agent removing or extending a rule without the owner.

### Decisions for package 10

| # | Question | Recommended | Why |
|---|----------|-------------|-----|
| D10-1 | A new tool, or `approval_request` with type `gate.trust`? | `approval_request` | One way to ask for anything; `approval_list_actions` already describes the type to agents |
| D10-2 | Exact match only, or domains too? | Exact only | A domain rule for `@example.org` lets an agent reach people the owner never named |
| D10-3 | End date | 90 days by default, 365 at most | A rule nobody remembers should not last for ever |
| D10-4 | Step-up sign-in to approve a rule | yes | It loosens protection, as `SEND_REQUIRES_APPROVAL=false` does |
| D10-5 | Daily limit per rule | 20 | Bounds what a misled agent can send to one person before the owner looks |
| D10-6 | Push for released requests | no | The owner asked not to be asked; Activity shows each one |

Reviewed on 2026-11-18 with gstack `/plan-eng-review` and a second opinion from Codex (gpt-6-astra). Taken in: the per-type
step-up, `gate.trust`'s own list check and the use table from the review; from Codex, `standing_identity` instead of
`recipients()`, waiting on an empty identity, one try per request, one transaction for rule, use and claim, and `gate.submit()`
with the push only for waiting requests.

The dashboard parts (the settings list and the Activity marker) need a mock before they are built. The card of `gate.trust`
uses the general card of actions and needs none.

Acceptance criteria:

1. Each row of the standing release table has a test against a made-up type. The test checks the state, the answer to the agent,
   how many times the executor ran, and that a request with one uncovered recipient waits. A type whose `standing_identity`
   answers `None` or `[]` waits even when rules exist.
7. Asking again with the same bytes or key after a `failed` release leaves the request waiting and runs the executor no more.
8. Two requests racing for the last use of a day: exactly one is released. A rule removed before the transaction releases nothing.
9. A request released by a rule sends no push; one that waits sends one, as today. Each tool that queues answers through
   `gate.submit()` (a test per tool against its real answer, not a fake type).
2. A `gate.trust` request for an unknown type, for `gate.trust`, for a type that has not opted in, for a recipient outside the
   type's list and for a domain is refused with `policy`; nothing is queued.
3. Approving a rule without a recent sign-in is refused and the request keeps waiting; with one, the row exists and a request to
   that person is released.
4. An expired rule, a removed rule and a rule past its daily limit all leave the request waiting.
5. The log line of a standing release names the rule and no recipient of a non-builtin type.
6. Rollback: the version before package 10 starts on the same `DATA_DIR` and ignores the table; every request waits again.

Effort, estimated and not measured: about 480 min of work (store, rule and use table 90, standing release in one transaction 90,
`gate.submit()` and moving every caller 90, `gate.trust` type and step-up 60, tests 150) after the mock, plus 60 min of review.

## Rules for whoever implements a package

1. An acceptance criterion is met by a test that states what it protects, which wrong result it rejects and what a green run
   does not prove.
2. Code and its tests land in the same change. A placeholder, a stub assertion or a test put off to later is not done.
3. Any change to a test, a golden file, a timeout, a threshold or a suppression is reported on its own line, with its reason,
   before any other result.
4. A reported command comes with what it printed: exit status, counts, the failing line.
5. One commit touches one bin of `dev/provenance.toml`, and every commit passes the full suite. A change that needs the `gate`
   and a `mixed` file lands as two commits, gate first; a move touches the bins it moves between and nothing else.
6. `CONTRIBUTING.md` applies in full: made-up data only, noreply author, no co-author trailers, `python dev/privacy_check.py`.

## Merging upstream

The old module paths are aliases now, so git does not see `outbox.py` and `audit.py` as moved. A change upstream makes to either
arrives as a conflict on the alias file. Keep the alias and carry the change into `gate/` by hand; resolving the conflict either
way without that drops the change silently (keeping the alias) or brings back a second copy of the module (taking upstream's
file, which `tests/test_provenance.py` reports). After each merge, compare what upstream changed:

```bash
git diff ORIG_HEAD...MERGE_HEAD -- src/icloud_mcp/outbox.py src/icloud_mcp/audit.py src/icloud_mcp/dashboard.py
```

## Rollback

| Package | How to undo |
|---------|-------------|
| 0 | Revert the commit; it moves files and adds two tests |
| 1 | Revert; no stored data changes shape. Waiting requests of a type that is gone show as "Unknown action" and can be discarded, as today (`external.py:77-80`) |
| 2 | Unset `GATE_ACTIONS_FILE`; the types are not registered and their waiting requests show as above |
| 3 | Stop the server, run `python -m icloud_mcp.gate.export_queue DATA_DIR` (in the image: `docker run --rm -v <volume>:/data <image> python -m icloud_mcp.gate.export_queue /data`), start the previous version. The exported requests end in the store, so a later upgrade reads the file in again and never revives one the old version sent. Ended requests and their keys are lost to the previous version; their log lines stay |

## Effort

Rough estimates for an agent doing the work and the owner reviewing; nothing was measured.

| Package | Work | Review by the owner |
|---------|------|---------------------|
| 0 | 90 min: 40 moving and re-exports, 30 provenance file and its test, 20 import rule | 20 min |
| 1 | 240 min: 60 `ActionSpec` and registration, 80 the two tools, 100 tests | 45 min, with one click through the dashboard per state |
| 2 | 300 min: 60 moving the client, 120 file schema and executor, 120 tests with the fake gateway | 45 min, plus one real request to a test conversation |
| 3 | 360 min: 120 store, 60 migration and export, 60 log hash, 120 tests | 60 min, on a copy of a real `DATA_DIR` first |

## Out of scope

- A second owner on one instance, roles, or approval by more than one person.
- Approving from a chat app or by mail. The owner approves on the dashboard.
- A policy language. A list per action and a policy function per plugin stay the only rules.
- Reading from a service through the connector file. The file describes actions; reading stays with plugins.
- Any change to the Mac helper.

## Make-vs-buy

Checked on 2026-11-13 against the repositories and documentation, not run.

| Project | Why it does not replace the gate |
|---------|----------------------------------|
| [Preloop](https://github.com/preloop/preloop) (Apache-2.0) | The approver cannot edit the arguments, the approval is not bound to a hash, rules are not checked again at approval, and it returns a decision instead of carrying the action out. Eight containers with Postgres and NATS |
| [MCP Human Approval Gateway](https://github.com/trivediroshan1-ui/mcp-human-approval-gateway) (MIT) | Hash binding, single use and an unknown-outcome state are there, but its README calls it a teaching prototype: no real downstream, no sign-in for approvers |
| SentinelGate (AGPL-3.0) | Approval by a person is a paid feature, and the original repository returned 404; read from a [mirror](https://github.com/iflow-mcp/sentinel-gate-sentinelgate) |
| [LiteLLM](https://docs.litellm.ai/docs/mcp) | No approval per call at the gateway. `require_approval` hands the call back to the client. A `pre_mcp_call` guardrail could hold a call, and the queue, the page and the single execution would still be ours |

Make-vs-buy: built here, because no project that was checked combines editing before approval, an approval bound to the content's
hash, a second check of the rules at approval and carrying out actions that are not MCP tools. Two ideas are taken over: the
unknown-outcome state after a crash (package 3) and signed checkpoints with an export for the log (package 8). SQLite comes from
the standard library, and the gateway client is the one Beeper already uses.

## Questions for the upstream author

To be asked once packages 0 to 3 run in the fork.

1. Is a general approval gate something iCloud MCP should hold, or does it stay a server for iCloud and nothing else?
2. If yes: is a `gate` package inside the repository acceptable, with the import rule of package 0?
3. Which of the `mergeable` candidates are welcome as pull requests of their own, whatever the answer to 1?
4. Is SQLite for the queue acceptable, given that upstream's `outbox.py` is a JSON file of 129 lines?

## Writing a plugin action

Package 1 is built: a plugin offers actions of any kind through `Plugin.actions`, a tuple of `ActionSpec`
(`src/icloud_mcp/plugins/__init__.py`). `ActionSpec` and `PluginContext` are public from package 1 and versioned in package 7.

| Field | What it is |
|-------|------------|
| `action_type` | An `ActionType` (`gate/actions.py`): id, title, badge, approve label, the parameters as a JSON Schema, the rows of the card and the text the owner may edit. Its first row names who or what the request reaches; the card shows that row first, then the text, then the other rows. |
| `executor(ctx)` | What carries it out after approval. It answers `sent`, `failed` (provably never reached the service; the request waits again) or `unclear` (never repeated). |
| `allowed(ctx)` | Who it may reach, as the owner has it now. `*` means anyone and empty means nobody. Mail's `SEND_ALLOWLIST`, where empty means anyone, is the one exception until package 6. |
| `policy(ctx)` | Further rules on the parameters: a function that returns why a request is not permitted, or None. |
| `prepare(ctx, params)` | Optional. Asks the service what the target is and returns the parameters that are stored, so the card shows the service's words. Fields it adds are `readOnly` in the schema; an agent that sets one is refused. Raise `ExternalError(message, code, path)` to refuse. |
| `recheck(ctx, params)` | Optional. Called after the owner approved and before the executor. Return `target_changed` when the target is not what the owner saw: nothing is carried out and the request ends as discarded with that reason. |
| `hold(ctx)` | Optional. Whether approving is off right now: an `actions.Hold` with level `blocked` (the card says why and approving is refused, nothing is carried out, the request keeps waiting) or `note` (one line on the card), or None. Read on every look and every approval; one that raises blocks. |

`ctx.value(key, default)` reads one of the plugin's own settings. Agents name an action `action` in `approval_request` and
`approval_list_actions`; `approval_status` keeps the field `action_type` it already had, so existing agents read it unchanged.
`approval_request` checks the agent's own parameters (schema, size, the owner's list) before `prepare`, so the service is never
asked about a target the owner did not allow; the policy runs after `prepare`, on what it filled in. `readOnly` is allowed only
on top-level properties, and a type with `readOnly` fields needs a `prepare`. A `request_id` returns the first request only
while that one still waits; package 3 keeps keys after a request ended. The list and the policy are checked when a request is queued
and again when it is approved. Agents ask with `approval_list_actions` and `approval_request`; a type that a chat channel owns
(`messages_send_request`) is left out of the list and refused by name. `tests/test_plugin_actions.py` is a worked example.

## Connector file reference

Package 2a is built (`src/icloud_mcp/gate_connectors/`). A service behind the MCP gateway becomes an action through one JSON
file. `docs/examples/actions.example.json` is a working example with made-up data, and `docs/examples/actions.schema.json` is
the file's JSON Schema (the loader's own, kept equal by a test). Check a file with
`python -m icloud_mcp.gate_connectors check FILE`; add `--probe` to ask the gateway as well. Exit code 0 means fine, 2 the file
cannot be used, 1 the probe found a problem: the execution key misses a tool, an agent key (`GATE_AGENT_KEY_PROBE`) sees a send
tool, or an agent key could not be checked. A file that cannot be used stops the server start; elsewhere the connector is only
left out.

| Environment variable | What it is |
|----------------------|------------|
| `GATE_ACTIONS_FILE` | The file. Unset: no file actions. |
| `GATE_GATEWAY_URL` | The gateway's MCP address. Required with the file; a user or password in it is masked in every error. |
| `GATE_EXEC_KEY` | The key that asks the service and carries actions out. Required with the file; masked in every error. |
| `GATE_AGENT_KEY_PROBE` | Optional: one or more keys (comma-separated) with the grants agents have. Use a key that can only list tools. The server lists them at start and on every health check; an action whose send tool such a key can see cannot be approved until the grant is gone, and `--probe` reports it too. Unset: cards and health say the grants were not checked. |
| `GATE_PROBE_REQUIRED` | Optional, `true` to make "not checked" (no probe key, no run yet, a key that could not list) block approving like a bypassable action. Default: a note only. |

The file is one object: `$schema` (optional, the schema's address for editors), `version` and `actions` (the list). `version` is 1; the format is frozen at the keys below, and anything
more is a Python plugin (`ActionSpec`). Each action:

| Key | Required | What it is |
|-----|----------|------------|
| `action` | yes | Its id, lower-case words joined by dots, unique in the file. Agents pass it to `approval_request`. |
| `title`, `badge` | yes | The card's title (80 characters at most) and small label (20). |
| `approve_label` | yes | The approve button, 12 characters at most. |
| `tool` | yes | The gateway tool that carries it out. `describe.tool` must be another tool. |
| `arguments` | yes | What `tool` is called with, with placeholders (below). |
| `params_schema` | yes | The parameters as a JSON Schema object. Fields `describe` fills in are `readOnly`, only at the top level, of type string (with `maxLength`), number, integer or boolean. With `$params` in `arguments`, `additionalProperties` must be `false`. Only declared fields reach the service. |
| `text_field` | no | The parameter shown as the request's text, which the owner may edit before approving. |
| `editable_fields` | no | Further parameters the owner may change before approving (package 2d), edited in one form with the text. Each is set by the agent (not `readOnly`), of type string, number, integer or boolean, and shown in `rows`. A recipient field in the list lets the owner change who it reaches: someone typed in by hand who is not on the list is allowed for that request only. |
| `recipient_fields` | yes | The parameters that say who or what it reaches; checked against the owner's list. Each must be required and set by the agent (not `readOnly`). |
| `rows` | yes | `[label, field]` pairs of the card. The first row names who or what it reaches: a recipient field or a field `describe` copies. The labels To, Guests, Will be delivered to and Conversation are the gate's own. |
| `describe` | no | Asks the service about the target before the request is queued: `tool`, `arguments`, `copy` (readOnly field to dotted path of the answer) and `recheck` (copied fields compared again just before the send; a difference ends the request as discarded, target_changed). |
| `result` | yes | Reads the tool's answer: `path` (dotted), `sent` (values that mean done) and `failed` (values that mean it provably had no effect; the request waits again). Values match in type and value (`true` is not `1`), and no value may be in both lists. Anything else, an error, or no answer is unclear and never repeated. List under `failed` only what the service guarantees had no side effect. |
| `allow_setting` | yes | The setting that holds the owner's list for this action: ids, `*` for anyone, empty for nobody (the default). Its name ends in `_ALLOWLIST`, does not start with `GATE_` and is not one of the core's or Beeper's lists. Editing it on the dashboard comes with package 2b (S5); until then it is an environment variable. |
| `enabled` | no | `false` keeps the action in the file without offering it. |
| `timeout_seconds` | no | How long one call may take, 1 to 120, default 30. |

Placeholders replace whole string values only:

| Placeholder | Becomes |
|-------------|---------|
| `$params` | Every parameter the agent may set (the readOnly ones left out). |
| `$params.<name>` | One of them; left out of the call when the agent did not set it. A readOnly field is refused. |
| `$attempt_key` | `<outbox_id>.<attempt>`: a new key for each execution, so a gateway that dedupes on it sees one key per attempt. Not in `describe`. |
| `$$...` | A literal `$...`. Any other value that starts with `$` is refused when the file is read. |

| The call | The request |
|----------|-------------|
| The gateway could not be reached, or refused the key before the call | failed: it waits again |
| The answer's `path` value is in `sent` | sent |
| The answer's `path` value is in `failed` | failed: it waits again, and the next execution gets a new `$attempt_key` |
| Anything else: another value, no value, an error answer, no answer after the call left | unclear: never repeated |
| `describe` fails when an agent asks, or a copied value does not fit its field | refused, `describe_failed` (retryable); nothing queued, and nothing of the answer shown |
| `describe` fails just before the send | not sent; the request waits again |

A file that cannot be used stops the start with the file, the JSON pointer of the place and the check command; so does the
file without `GATE_GATEWAY_URL` or `GATE_EXEC_KEY`. Whoever writes the file is trusted: it decides which tools are called.

D1, the LiteLLM spike (2026-11-15, LiteLLM `main` at d49706f, v1.110 docs): the `pre_mcp_call` hook sees the tool, its arguments
and the caller, but it can only change the arguments or headers or raise; it cannot answer a call with a queued result, and
LiteLLM keeps no store or replay path for a call approved later. One of three, so the file connector stays.

## Refusal codes

`approval_request` refuses with one JSON object, raised as a tool error: `{status: "refused", sent: false, reason, message, fix,
docs, path?}`. Nothing was queued. `message` never repeats an entry of the owner's lists, and a service's own error text never
reaches the agent.

### Refusal codes: unknown_action

No action of that name on this server. Fix: call `approval_list_actions` and use one it lists.

### Refusal codes: channel_tool

The action belongs to a channel's own tool, named in the message (a chat message goes through `messages_send_request`).

### Refusal codes: schema

A parameter does not fit the action's schema, or `params` is not an object. `path` names the parameter.

### Refusal codes: read_only

The agent set a field the server fills in from the service. `path` names it; leave it out.

### Refusal codes: not_allowed

Who the request reaches is not on the owner's list. Only the owner can change the list.

### Refusal codes: policy

A rule of the owner's or of the action's type does not permit it. Only the owner can change it.

### Refusal codes: describe_failed

The service could not be asked about the target. Adds `retryable: true` and `side_effects: "none"`; asking again later may pass.

### Refusal codes: key_conflict

The `request_id` was already used for a request with other parameters. Use a new key, or ask with the same parameters.

### Refusal codes: not_replaceable

`replaces` names no request of this agent's that still waits for the same action, or one with another `request_id`. Nothing was
changed. Ask without `replaces` for a new request.

### Refusal codes: queue_full

Too many requests are waiting for the owner. Do not retry; tell the owner.

### Refusal codes: store_unavailable

The approval store could not take the request (a full disk, a database busy for too long). Adds `retryable: true` and
`side_effects: "none"`; asking again later may pass.

## Review obligations

A review on 2026-11-13 (`/autoplan`: CEO, design, developer experience and engineering) accepted the obligations below. They
bind the package each one names and win over the package text above where the two differ. The ids are kept so pull requests
can cite them.

### Direction and scope

- E1 (package 2, folded into its bullets, criteria, tests and effort): an always-reported agent-key check (not a start blocker) per action from `GATE_ACTIONS_FILE`. `GATE_AGENT_KEY_PROBE` takes one or more keys (comma-separated) with the grants agents have. It runs at start and on every `icloud_check_health` call; cards read the last result held in memory; before the first probe finishes, or when the gateway cannot be reached for it, the card and health say "agent grants not checked" as a warning. The card warning goes through the action type's existing `warnings` function (`gate/actions.py:71`), so the gate learns nothing about gateways. Unset: health reports "agent grants not checked" as a warning (the server still starts). A probe key that can list the action's `tool`: health reports a problem and the card warns that agents can bypass the gate. Tests: unset gives the warning; a fake gateway that lists the send tool to a probe key gives the problem line and the card warning; two keys, one of them bypassing, give the problem; clean keys give ok; no probe key value appears in any error, log line or answer. `no_claim`: listing is checked, not calling; a gateway that hides a tool from the list but still allows the call passes this check. Effort +60 min work, +10 min review.
- E2 (package 2): an action whose allow setting is empty shows "reaches nobody yet" in that connector's settings section on the dashboard (where Beeper's `BEEPER_SEND_ALLOWLIST` is edited today, `plugins/beeper/__init__.py:51`). Test: empty shows the hint; `*` or one entry removes it. Effort +30 min.
- S5 (package 2): each `allow_setting` is registered at start as a setting of the gateway connector's own settings section, the way Beeper's plugin registers `BEEPER_SEND_ALLOWLIST`; the gate itself imports no `settings`. Widening the list (adding an entry or `*`) counts as loosening, like the Beeper list: the dashboard asks the owner to confirm with a recent sign-in (`settings.py:391`, `:509`, `api_need_confirm`). Test: widening without a recent sign-in is refused with the confirm answer; narrowing is not.
- S1 (package 2, overrides plan line 276): when a `recheck` field differs at execution, nothing is sent and the request ends as `discarded` with the reason "the target changed since you looked; ask again". It is not `failed`, because `failed` would put it back unchanged and every later approval would fail the same way. The log gets `approval.discarded` with that reason and the request's `sha256`; `approval_status` answers `discarded` with the reason. Test: a title change at execution sends nothing, the request is not waiting afterwards, and a fresh `approval_request` queues a new card with the new title.
- S2 (package 2): `describe` failing or timing out at queue time refuses the request (nothing queued, the answer says why). At execution, before the send, a `describe` failure or timeout gives `failed` (nothing was sent) and the request waits. One test for each.
- Cl1 (package 2): `$request_id` is `<outbox_id>.<attempt>`, where `attempt` counts executions of this request (1 for the first, +1 after each `failed`). It is never the agent's optional `request_id`. A retry after `failed` therefore reaches the tool with a new key, while a gateway that dedupes on the key still sees one key per attempt. Test: `not_sent`, then a second approval, reaches the fake gateway twice with keys `.1` and `.2`.
- K1 (package 3): an external request's lasting key ends when its row is deleted after `AUDIT_KEEP_DAYS`; after that the same `request_id` queues again. Intended; test with a row aged past the limit.
- Cl2 (package 1): in `ActionSpec.allowed`, `*` means anyone and empty means nobody. Packages 4 to 6 must reconcile mail's `SEND_ALLOWLIST`, where empty means anyone.
- Cl3 (package 1): `readOnly` is enforced by explicit code before schema validation; `jsonschema` does not refuse a `readOnly` field by itself.
- C3/S3 (package 3): the `Outbox` interface changes: the constructor takes `source` instead of `filename` (call sites `server.py:1346`, `:2628`, `:3020`), and `claim` gains the target state (`claim(id)` for discard, `claim(id, "executing")` for approval; this replaces `claim(id, "execute")` in the package 3 transition table). `OUTBOX_MAX` stays a per-source limit. Test: a full mail queue does not block an external request.
- F-CEO-4 (package 3): `Outbox.add` keeps `approval_page` and the full-queue message that names it (`gate/outbox.py:130,145`). Test: a full queue's error names the page passed in.
- F3 (package 3 rollback row): after a rollback through `dev/export_queue.py`, ended requests and their lasting keys are gone; a repeated `request_id` queues again.
- F-CEO-5 (package 0 text): `callctx` is named next to `textedit` as a tie that keeps the gate inside this server (`gate/outbox.py:22`, `gate/withdraw.py:36`).
- C4: the owner kept the original order: every package is built and run in the fork first, and upstream is asked afterwards.
- Deferred, not in this plan: E3 per-action daily cap; E4 reusable fake-gateway fixture (with package 7). Skipped: E5 proxy reframing (taste D1).
- K0 (before the first real package-2 action): agents' LiteLLM keys lose every write tool the connector file names; only `GATE_EXEC_KEY` keeps them. An owner task in the gateway's own configuration, outside this repository. Verification: `icloud_check_health` reports the probe keys clean for every action.
- D3 (accepted; package 2): an action whose `tool` a probe key can list is registered, but its approval is refused with "agents can call this tool directly; remove the grant first". Test: approval of such a request sends nothing and the card says why; after the probe turns clean, approval works.
- D1 (accepted; before package 2): a written 60-minute spike of the LiteLLM `pre_mcp_call` hook (what it sees, whether it can hold a call and return a queued result, how the idempotency key maps), from LiteLLM's own docs. The config-file route stays unless the spike shows all three.
- N4 (package 2): connector file format version 1 is frozen at the keys in the plan; anything beyond goes into a Python plugin through `ActionSpec`. A second real service is named in the PR before package 2 merges. Packages 8 and 9 run only on demand.
- D5 (accepted; package 3): the store takes the `external` source first; mail, calendar and iMessage move to it with packages 4 to 6. Criteria 5, 6 and 8 of package 3 then apply to `external-outbox.json` only until those packages.
- N7: at the start of each package, make-vs-buy is re-checked against gateway HITL features, one dated line in the PR.

### Cards and states

- H1 (package 1): one card order for every action type, set by the renderer: badge and title, recipient row (from `describe` copies, else a `recipient_fields` row), text, other rows, warnings, time left, Discard and the approve button. Load check (package 2): `recipient_fields` without a readable row stops the start. Test: a file-defined card renders in that order; the load check fails on a file without a recipient row.
- H2 (package 2): health lines sort problem, warning, ok; ok lines fold into one count line. Test: one bypassable action among four shows first.
- S-a (package 2; replaced by package 2c, owner 2026-11-15): after a recheck discard, the item stays on Home for the session as "Not sent: the target changed since you looked. Your assistant has to ask again." Test: dashboard state after S1 shows it.
- S-b/S-c/S-d (packages 1 and 2): file-defined actions use DESIGN.md's After Approve states; the unclear line comes from the type ("Not sure it reached {badge}. Nothing will be sent again; look there first."), replacing wording that names Messages or a conversation; a `failed` return shows "Did not go out: {reason}" above the buttons. Test per state.
- S-e (package 2, with D3): while D3 holds, the approve button is off and the card shows the D3 sentence with a link to health; "agent grants not checked" is a note and never blocks. Test: both cards.
- S-g (package 1): an unknown action shows its stored type id and age, with Discard. Test.
- S-h (package 1): `approval_list_actions` with no actions returns `{actions: []}` and a one-line hint. Test.
- J1 (package 2): the E2 hint links to the list field; the loosening confirm names the action. Test.
- B1 (package 2): `badge` renders with the existing `.tag` in one neutral style; `approve_label` at most 12 characters, checked at load. Test.
- A1 (package 1): one refusal shape for agents, `{status: "refused", sent: false, reason, message, path?}`, codes `unknown_action`, `schema`, `read_only`, `not_allowed`, `policy`, `channel_tool`, `describe_failed`, `key_conflict`, `queue_full`; one test per code.
- C1 (packages 1 and 2): every new owner and agent string is listed in one table in the PR and passes the humanizer skill before build.
- P1 (before package 1 UI code): one standalone HTML mock of the card in every state (waiting, edited, sending, sent, unclear, did-not-go-out, discarded-by-recheck, warning note, blocked by D3, unknown action) and the settings hint, approved by the owner; the same list is the "one click per state" checklist.
- Deferred: S-f refusal counts on the dashboard.

### Agents and connector authors

- X1 (package 2): `python -m icloud_mcp.gate_connectors check <file> [--probe]` runs the same loader as the start, prints each action and its card preview, exits 0 or 2 with errors on stderr; `--probe` calls `describe` and the agent-key probe once. The start error names this command as its fix. Test: a broken file exits 2 with the JSON pointer; a good file exits 0.
- X2 (package 2, supersedes deferral E4): the fake gateway used by package 2's tests lives in `dev/fake_gateway.py` and is usable by a connector author. Test: package 2's tests import it from there.
- X3 (package 2): `docs/examples/actions.example.json` with made-up data; a test loads it with the real loader.
- X4 (package 1): `icloud_check_health` says "approval tools off: the dashboard is not enabled" when the tools are not registered. Test.
- X5 (package 2): the file's substitution is `$attempt_key` (value `<outbox_id>.<attempt>`, per Cl1); `request_id` names only the agent's key. Test.
- X6 (packages 1 and 2): the action identifier is `action` on every surface an agent or file author sees (the file key `id` becomes `action`); `action_type` stays internal.
- X7 (package 1 docs): the allow-list rule is written once: empty means nobody, `*` means anyone; mail's `SEND_ALLOWLIST` (empty means anyone) is named as the exception until package 6.
- X8 (package 2): substitution replaces whole values only; `$$` is a literal `$`; paths are dotted with no array index in v1; anything else is refused at load. One test per rule.
- X9 (package 1): the `channel_tool` refusal names the tool to use; `approval_list_actions` leaves channel-owned types out.
- X10 (package 2, with D3): a request for a bypassable action is queued, its answer carries `warnings: ["bypassable"]`, and approval is blocked (S-e); `bypassable` is removed from the refusal codes.
- X11 (package 1): `PluginContext` is defined with its fields; `prepare(ctx, params) -> params`; `ActionSpec` and `PluginContext` carry a note "public from package 1, versioned in package 7".
- X12 (packages 1 to 3): every A1 refusal has a `fix` line and is raised as `ToolError` (MCP `isError`), like the repo's other tools; `describe_failed` adds `retryable: true` and `side_effects: "none"` and passes on only the gateway's error class and status, never its body; start errors name the file, JSON pointer, env var and fix command and exit non-zero; a set `GATE_ACTIONS_FILE` with a missing `GATE_GATEWAY_URL` or `GATE_EXEC_KEY` stops the start naming the variable; userinfo or tokens in `GATE_GATEWAY_URL` are masked; an executor exception is logged as class name plus `outbox_id` only. Tests: one per code checks `fix` and the error flag; a gateway body marker never reaches the agent; a raised exception carrying a key or a param value leaves neither in the log.
- X13 (package 1): `docs/SENDGATE.md` gets a "Refusal codes" section with one anchor per code; each refusal links it.
- X14 (packages 1 and 2): docs sections "Writing a plugin action" and "Connector file reference" (every key, substitution grammar, the outcome table failed/unclear/sent, env vars, example); the JSON Schema of the file is published next to the loader and `$schema` is accepted. A test checks that every schema key and every refusal code appears in the docs.
- X15 (package 3): migration copies the JSON entries into the table in one transaction, then renames each file to `.migrated`; on restart, a `.json` next to rows with the same ids is reconciled by id and hash and never imported twice; a corrupt JSON stops the start naming the file and `dev/export_queue.py`. SQLite runs with a `busy_timeout`; a full disk refuses new requests as `store_unavailable` (retryable, side effects none); the start refuses a database file readable by others. `delivery` is documented as mandatory on every `approval.sent` line. Tests: crash between commit and rename; busy; mode check.
- X16 (package 2): `result` is required for every action in file format v1. Test: a file without `result` stops the start.
- D6 (accepted; package 2): v1 adds optional per-action `enabled` (default true) and `timeout_seconds` before the format freezes (N4); per-action expiry stays out.
- X17 (end of package 2): the owner measures TTHW once (example to card with the fake gateway) and records it in the PR.

### Engineering

- E-1 (package 3, replaces the "corrupt JSON stops the start" part of X15): a corrupt queue file at migration is renamed `<name>.corrupt-<time>`, never imported or sent; the start continues; health shows a problem naming the file. Test.
- E-2 (packages 1 and 2): `Registration` gains optional `recheck(params) -> reason | None`, called by `release` after the claim and before `execute`; a reason ends the request `executing -> discarded` with `outcome_reason = "target_changed"`. The executor contract stays sent/failed/unclear. `approval_status` for `target_changed` says "the owner did not see this target; ask again only if the owner still wants it". Test.
- E-3 (package 1, moved from package 3): every log line of a request carries its `sha256`; `approval.edited` the new hash, `approval.edit_undone` the restored one. Test: edit once, approve: three lines with first, second, second hash.
- E-4 (packages 2 and 3): `attempts` counts executions; JSON entry writes it only when above 0 (golden bytes unchanged); SQLite `attempts INTEGER NOT NULL DEFAULT 0`, bumped inside the claim. Test.
- E-5 (package 3): exclusive `fcntl.flock` on `DATA_DIR/approvals.lock` at start; a second process refuses to start naming the lock; crash recovery runs only under the lock. Test with two processes.
- E-6 (package 2): queue order is schema, readOnly, allow list, policy, then `describe`; X1 `--probe` warns when `describe.tool` equals an action `tool`; docs say the file author is trusted. Test: a non-allowed id never reaches the fake gateway.
- E-7 (package 2 docs): recommend a list-only probe key that mirrors the agents' grants.
- E-8 (package 0 text): replace "`git log --follow` still finds each file's history" with `git log --follow -M -C` or drop it; name the transitive `textedit -> mail` import next to F-CEO-5 as a known edge removed by package 6.
- E-9 (all packages): every commit passes the full suite; a change that needs `gate` and a `mixed` file lands as two commits, gate first.
- E-10 (package 3): `key_fingerprint = sha256(canonical(params without text_field))` stored at queue time and kept after the content is NULLed; a repeated key with other params against an ended row answers `key_conflict`. Test.
- E-11 (package 3): `CREATE UNIQUE INDEX request_key_external ON request(key) WHERE source = 'external' AND key IS NOT NULL`; mail, calendar and iMessage keep a non-unique index; lookup and insert in one `BEGIN IMMEDIATE`; one connection with `check_same_thread=False` under the existing RLock. Test: two threads, one key, one row.
- E-12 (package 3): expiry and the `AUDIT_KEEP_DAYS` deletion run lazily under the lock on every read and write (today's `_prune` pattern); `approval.expired` with `sha256` written exactly once; a `failed` return past `expires_at` ends as `expired`, not `waiting`. Tests.
- E-13 (package 3): `dev/export_queue.py` refuses while any row is `executing` and prints how many ended and `unclear` requests the rollback loses. Test.
- E-14 (package 2): one `list_tools` per probe key per probe run; the start finishes while the fake gateway hangs. Tests count calls and time the start.
- E-15 (package 3): `outcome_reason TEXT` holds a code, never a parameter or text, and survives NULLing; WAL `-wal` and `-shm` files are mode 600. Tests.
- E-16 (package 2 docs and X1): values in `result.failed` must be ones where the tool guarantees no side effect; `check` prints the mapping for the operator to confirm. AC4 `no_claim`: the recheck narrows, not closes, the window before the send.
- D7 (accepted; package 3): an external key stays bound for `waiting`, `executing`, `sent`, `unclear`, and for `discarded` by the owner and `withdrawn` (answering the status record); it is freed after `discarded` with `target_changed` and after `expired`, so the agent can ask again. Tests for both paths.
