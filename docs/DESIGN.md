# Dashboard design (fork)

The owner dashboard is an Ionic Core app at `/dashboard`. It talks to the server only through the JSON API under
`/dashboard/api`. This file holds the rules every change to it follows.

## Decisions

| Decision | Why |
| --- | --- |
| Ionic Core 9 in iOS mode is the only UI library | One library for every control, so no screen grows its own buttons, toggles or sheets. It works in plain JavaScript without a build step |
| No hand-made controls | Buttons, toggles, segments, inputs, selects, lists, alerts, toasts and sheets are Ionic components. Own markup is limited to the pieces in "Own pieces" below |
| Vendored and pinned (`static/vendor/`) | The CSP allows `script-src 'self'` only, and the server reaches no CDN. A version bump is its own change, with every screen checked on a phone and a desktop |
| A JSON API instead of HTML forms | Ionic's controls do not take part in plain form posts: `ion-button` submits without `name`/`value`, and `ion-toggle` keeps its checkbox in the shadow DOM. Sessions, CSRF, passkeys, approvals and the audit log stay on the server |
| Web Push without pywebpush | pywebpush pulls in requests and aiohttp. Its building block http-ece ships only as a source package, which the image build cannot fetch. `push.py` uses `cryptography` and PyJWT, which the server already has, and `tests/test_push.py` checks it against the worked example in RFC 8291 |
| English everywhere | The code and upstream are English. Sample data in mocks and tests is English and made up |

## Look

Neutral and quiet, with one accent. Dark and light follow the system (`prefers-color-scheme`); every role has a value in
each, set once in `app.css` and mapped onto Ionic's variables, so the components pick them up without per-screen colour.

| Token | Light | Dark | Used for |
| --- | --- | --- | --- |
| `--ground` | `#fbfbfc` | `#1c1c1d` | page |
| `--side` | `#f3f3f5` | `#181819` | the side list on wide screens |
| `--surface` | `#ffffff` | `#212123` | lists, the message card, the request pane |
| `--raised` | `#f1f1f4` | `#29292c` | a card on a card, the selected row, the quiet button |
| `--border` | `#e4e4e8` | `#2e2e31` | every hairline |
| `--text` / `--muted` | `#1c1c1f` / `#6a6a72` | `#ececee` / `#9a9aa1` | running text / sublines, notes |
| `--accent` (Ionic primary) | `#5b66e8` | `#5b66e8` | the one filled button, switches, the sent bubble |
| `--accent-ink` | `#4650d4` | `#98a1f7` | text buttons and links (the accent itself is too light for small text on dark) |
| `--positive` / `--negative` | `#1f8f5f` / `#c93a32` | `#52c48f` / `#ee6a62` | On, Remove, a failed send |
| `--note-*`, `--ok-*`, `--neg-*` | amber, green, red tints | the same, darker | warnings and notices, as a background with its own text colour |

- Titles are bold system sans (`.title h1`, 30px, weight 700), one per screen, with one friendly line under it. Section heads
  and everything else use the same sans. There is no serif.
- No small uppercase labels above titles, no icons in tinted boxes, no shadows on things at rest. The tab bar is fixed at the
  bottom; its active tab takes the text colour, not the accent.
- Buttons: one filled button per bar (the accent), the other is Ionic's button with the `neutral` class (raised, hairline).
  With a warning on a request, Discard is the filled one and "Send anyway…" asks first. Danger is tinted text, not a filled
  button. Text buttons use Ionic's `fill="clear"`.
- Letters in a tile (`.av`) stand for an account; there are no provider logos. Icons are the ten Ionicons in
  `static/vendor/ionicons`; search, plus and check marks are not icons here.
- A push note says only what kind of item waits, never who it goes to or what it says. Neither does the log: it records that
  a text was edited, never the text.

## Screens

| Screen | Element | Content |
| --- | --- | --- |
| Home | `icm-today` | Greeting by time of day, one sentence on what waits and when the oldest is dropped, the live line, notices (result, paused, burst, a request your assistant withdrew in the last 24 hours, install tip), Waiting (kind tag, group tag, time left), Sent today |
| One request | `icm-item` | Title, one line on who asked and when (and "Edited by you"), the warning, the mail it answers, missing files first, the message as it goes out with "Edit text", the text the agent asked for with "Bring it back" when it was edited, attachments (tap for preview), and Discard plus the approve button. When there is a warning, Discard is the main button and "Send anyway…" asks first |
| After Approve | `icm-item` | Sending (the buttons are off), sent (back to Home with the result), unclear (Messages did not confirm it: nothing is sent again, the page stays until Done), not sent (the request still waits: Try again or Discard) |
| Edit text | `icm-item`, `edit.js` | The message card with its text as a text field, a character count, Cancel and Save changes. Only the text changes. A mail with an HTML part cannot be edited and says why |
| Activity | `icm-activity` | Filter segment, entries per day in plain words with a tag where one helps (Edited, Unclear, Withdrawn), chain check and download |
| Settings | `icm-settings` | One group per section: sign-in and security, connected accounts, iCloud, your Mac (live state of the helper), safety switches, account and server. On a wide screen a list of the sections replaces the side list, with "Back to app" |
| Connected accounts | `icm-accounts` | The accounts the setup assistant saved (provider, address, areas, read only), read only, with a link to `/setup`. Where an area has more than one account, the default one carries a "Default for …" tag |
| One area | `icm-group` | The area's fields as Ionic controls, Save in the toolbar, reset per field |
| Notifications, Passkeys, Connected apps, Owner password, Server | `icm-*` | One job each |
| Sign in / Confirm it is you | `app.js`, `ui.js` | Passkey first, then the owner password |

At 900px and wider, the tabs become a side list (Home with its count, Activity, Settings, the count waiting by kind, the pause
button and a state card), and Home shows the chosen request beside the list.

## Own pieces

Ionic has nothing for these, so they are written here, each in one place in `app.css`:

- `.title`, `.fresh`, `.notice` and `.note`: text blocks
- `.msg`: the message exactly as it goes out, with `.bubble` for an iMessage and `.capr` for the line under it
- `.warnbox`: why a request needs a second look
- `.tag` and `.av`: a small label and a letter tile
- `.slot`: the drop area for a missing file
- `.footbar`: the bar under a request on a phone. It is a plain bar, not an `ion-toolbar`, because Ionic restyles buttons inside a toolbar
- `.split`, `.side` and `.setnav`: the wide-screen layout

## Links into the app

- `#/item/<kind>:<id>` opens one request. Tools give this in `approve_at` (`Settings.approval_link`), and push notes open
  it too.
- `#/activity` and `#/settings` open those tabs.
- Old addresses (`/dashboard/approvals?item=`, `/dashboard/attach/<id>`, `/dashboard/security`, `/dashboard/settings`)
  redirect to them.

## Accounts model

`DATA_DIR/accounts.json` is version 2: `accounts` (each with a random `id`, `provider`, `auth`, `areas`, `read_only` and its
sign-in), `defaults` (area to account id) and `calendars_visible`. An area can be held by several accounts and has exactly one
default; the server runs with the defaults, so `Settings` still knows one account per area and nothing else had to change. A
file with no `version` is read as version 2 in memory and rewritten only when the owner saves on /setup (no copy is made); where
such a file lets two accounts hold one area, the account listed last keeps it, as it always did. An id is random and says
nothing about the address. `auth` is `app_password`; `oauth` is reserved and nothing reads it. `accounts.problems()` checks
that ids are unique, that every default is an account holding that area and that every held area has a default, and `save()`
refuses a model that fails it. /setup still offers one account per area, and the dashboard marks a default only where an area is shared.

How a second account would attach (none of this exists yet). Tools take an optional `account`, an id from the owner's list and
never an address; `settings_for_account(s, id)` already builds the settings for it, and without the argument the default of the
tool's area applies, so today's calls read the same. A queued request gets `account_id`, set from the account the tool
resolved: the owner sees the account on the request, Approve sends through that account, and the log records the id, never the
address; an entry without one belongs to the default of its kind. The connection pools and caches (IMAP sessions, CalDAV clients,
folder lists) live in the services, so they are built per account id on first use and closed when the file changes, with small
pools so a second account does not double the open connections. The list of correspondents behind `mail_find_correspondent` is
read from one mailbox and is kept per account id, never merged. Error scrubbing is built from one `Settings` today; with several
accounts it must take the secrets of every stored account, not only the defaults'. `calendars_visible` is one list for the whole
file and would become a field of the account.

## Hub

The approval outbox is meant to grow into one place where the owner approves actions of any kind. The rule stays as it is:
an agent or an outside system can only ask, only the owner approves, and the server carries it out. So far the core and the
registry exist, and one connector uses them: Beeper (below).

| Layer | What it is | Where |
| --- | --- | --- |
| Core | The outbox, the list and card on the dashboard, editing the text, approve, discard, expiry, withdrawing, push, the activity log and `approval_status` | `outbox.py`, `dashboard*.py`, `approval_*.py`, `textedit.py`, `audit.py` |
| Connectors | An action type (how a request looks, its parameters as a JSON Schema, which one is the editable text, whom it reaches) and an executor that does the work, registered together with the list of people it may reach | `actions.py`, `external.py` |
| Inputs | How a request arrives. Today `queue_external`, and the Beeper tool is its one caller; a further connector or an input would be another | `external.py`, `beeper.py` |
| Outputs | What leaves after a decision: the executor's call and, later, a message about a status change | later |

What every layer keeps:

- One instance per person, with its own `DATA_DIR`. Nothing is shared between owners.
- An entry is the canonical JSON of `{action_type, params}`; its hash is taken over exactly those bytes, so an edit changes the
  hash and the old version can no longer be approved, and bringing the agent's text back gives the first hash again.
- The status names are `waiting`, `sent`, `discarded`, `withdrawn`, `expired`, `unclear` and `unknown`, and a record is
  `{id, kind, status, edited, at}` (external requests add `action_type`). An executor answers `sent`, `failed` or `unclear`:
  `failed` means the other side provably never got the request and is the only answer that puts the entry back; `unclear` is
  never repeated, and an executor that raises counts as `unclear`. A new input maps its own words onto these names and this
  shape; it does not add its own. A status a caller does not know reads as "not sent".
- Log, audit, push and any message to an outside address carry the kind, the id, the outcome and the time, never parameters or
  text. The titles on the card come from the type, not from a parameter. The list of people a type may reach belongs to the
  owner; an empty list allows nobody, and it is checked again when the owner approves.
- A key makes queueing idempotent: the same key with the same request returns the entry that already waits (the text may
  differ, because the owner can have edited it), and the same key with another request is refused.

How a connector for the gateway (`mcp`) would attach: one action type per tool, with the tool's input schema as its parameter
schema. The executor calls the tool through the gateway with the parameters as the owner approved them. It answers `failed` only
when the gateway says the call was never made (refused before it was dispatched); once the call has left, a timeout or an answer
it cannot read is `unclear`. The gateway holds the credentials; the server holds the address and the owner's list of whom a
type may reach.

How `webhook` would attach, in two places. As an input, a signed request from outside becomes one `queue_external` call: the
signature and the sender's rate are checked first, the delivery id is the key (so a repeated delivery is the same entry), and the
answer is the entry's id. As an output, a status change of an entry is sent to an address the owner configured, with the kind,
the id, the status and the time, in the shape of an `approval_status` record. A webhook can also be an executor (a request to
the other side), with the same rule: `failed` only when no connection was made, `unclear` once the body has left.

Where the seam is not general yet:

- The kind tables in `static/app` have no entry for `external`, so a card's tag shows the raw kind, and the "not sure it
  arrived" notice talks about a conversation.
- `approval_withdraw` words its log line from a switch on the kind, so a withdrawn external request is logged without a title.
- A source's outbox is found by probing for `mail`, `cal` or `box` on it (in `dashboard.py` and `textedit.py`).
- `/outbox`, the page used when the dashboard is off, serves mail and iMessages only. External requests need the dashboard,
  as calendar changes do.

## Beeper

`beeper_send_request(chat_id, text, reply_to, request_id)` asks the owner to approve a message on a network behind Beeper. It exists
only when `BEEPER_GATEWAY_URL` and `BEEPER_EXEC_KEY` are set. Calling it sends nothing: the request waits as an action of type
`beeper.message.send`, and the owner approves it on the dashboard, as with a mail. The server holds no Beeper token. It calls the
gateway (a LiteLLM MCP endpoint) with an execution key that has no user and two grants: `send_message` on a server entry of its
own, and `get_chat` on the Beeper entry. Agents lack the first grant, which is what keeps them from sending.

- The card shows the chat as Beeper describes it. The tool asks `get_chat` (no participant names, no messages) and stores the
  title, the network, group or single, and the number of people with the request. A group adds a warning with its size. The tool
  takes none of these as parameters.
- Just before the send, the executor asks `get_chat` again. If the title, the network, single or group, or the number of people
  differs from what the owner saw, it sends nothing. The entry keeps waiting with the reason, so the owner discards it and the
  agent asks again.
- `BEEPER_SEND_ALLOWLIST` lists the chat ids that may be written to (empty means nobody, `*` any chat). It is checked when the
  request is queued and again when it is approved. It holds ids because names repeat and change. Choosing a chat by name on the
  dashboard is not built yet, so for now the ids are typed or pasted.
- After an approval, `sent` means Beeper accepted the message. `failed` means nothing reached the send tool: the gateway could
  not be reached, it answered 401 or 403 before the call, or the chat changed. The entry goes back to waiting. `unclear` means
  the call left and no readable answer came back, or the tool answered with an error. It is never repeated, and the owner is told
  to look in the chat.
- `request_id` is the key of `queue_external`: the same key with the same request returns the first entry.
- Log, audit and push carry the kind, the id, the outcome and the time. The card's title is always "Beeper message". The text,
  the chat's name and its id appear only in the entry itself, which is removed with it.

Not built yet: choosing a chat by name on the dashboard, the last messages of a chat on the card (another person's words, so a
switch of its own), and an instance for a second owner, who would have to agree first.

## Looking at a change

`PYTHONPATH=src python dev/preview_dashboard.py` serves the dashboard with made-up data (a fake mailbox and Mac helper, a few
waiting requests) and prints the address and a throwaway owner password. Check every screen at 390 and 1280 px, in light and
dark, and click each button in each state it appears in.
