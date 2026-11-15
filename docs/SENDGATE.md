# Sendgate: one approval gate for what an agent sends

Status: reviewed and approved 2026-11-13 (see "Review obligations"). "Sendgate" is the working title the dashboard already
shows. Package 0 is built; the others are not.
"Current state" was read at commit `330c82f` (branch `mb/ionic-ui`), which left upstream at `184ffa8`.

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
| 3 | SQLite store with states, lasting keys and the hash in the log | gate | 0 | yes |
| 4 | iMessage on `ActionType` and `Executor` | connector | 1, 3 | later |
| 5 | Calendar invitations on the same | connector | 1, 3 | later |
| 6 | Mail on the same | connector | 1, 3 | later |
| 7 | `plugin_api` version 1; Beeper's settings leave the core configuration | gate | 1 | later |
| 8 | Signed checkpoints of the log, export | gate | 3 | later |
| 9 | Webhook as a way in and as a notice of status changes | gate | 3 | later |

```
0 Line ─┬─> 1 Actions ─┬─> 2 Gateway connector
        │              ├─> 7 plugin_api
        └─> 3 Store ───┼─> 4 iMessage ─> 5 Calendar ─> 6 Mail
                       ├─> 8 Log checkpoints
                       └─> 9 Webhook
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
  `dev/export_queue.py` writes the waiting rows back in the old format.
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
6. `dev/export_queue.py`, run after that start, writes files that the previous version loads with the same ids and hashes.
7. For one request that was edited once and then approved, the log holds three lines whose `sha256` values are the first hash,
   the second hash and the second hash again.
8. The 10 test files that name a queue file read through the store and pass.

| Layer | What | Count |
|-------|------|-------|
| Unit | Each transition, and each one that must be refused | +9 |
| Unit | Key rule per source | +3 |
| Integration | Crash recovery, two approvals at once | +2 |
| Integration | Migration from the golden files and back | +2 |

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
| 3 | Run `dev/export_queue.py`, stop, start the previous version. Requests that ended after the migration keep their log lines and lose nothing else |

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
- S-a (package 2): after a recheck discard, the item stays on Home for the session as "Not sent: the target changed since you looked. Your assistant has to ask again." Test: dashboard state after S1 shows it.
- S-b/S-c/S-d (packages 1 and 2): file-defined actions use DESIGN.md's After Approve states; the unclear line comes from the type ("Not sure it reached {badge}. Nothing will be sent again; look there first."), replacing wording that names Messages or a conversation; a `failed` return shows "Did not go out: {reason}" above the buttons. Test per state.
- S-e (package 2, with D3): while D3 holds, the approve button is off and the card shows the D3 sentence with a link to health; "agent grants not checked" is a note and never blocks. Test: both cards.
- S-g (package 1): an unknown action shows its stored type id and age, with Discard. Test.
- S-h (package 1): `approval_list_actions` with no actions returns `{actions: []}` and a one-line hint. Test.
- J1 (package 2): the E2 hint links to the list field; the loosening confirm names the action. Test.
- B1 (package 2): `badge` renders with the existing `.tag` in one neutral style; `approve_label` at most 12 characters, checked at load. Test.
- A1 (package 1): one refusal shape for agents, `{status: "refused", sent: false, reason, message, path?}`, codes `unknown_action`, `schema`, `read_only`, `not_allowed`, `policy`, `channel_tool`, `bypassable`, `describe_failed`, `key_conflict`, `queue_full`; one test per code.
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
