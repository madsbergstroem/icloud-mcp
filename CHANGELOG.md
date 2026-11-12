# Changelog

What changed in each release, newest first. The GitHub release notes carry the full detail and the upgrade steps.
Update the Mac helper before the server whenever its version changes.

## Unreleased

- **Beeper's networks in Settings › Accounts (fork).** With `BEEPER_READ_KEY` set (a gateway key that may call Beeper's
  `get_accounts`; `BEEPER_ACCOUNTS_TOOL` names the tool, default `beeper-get_accounts`), the list shows each network Beeper is
  signed in to, with your name there and Beeper's account id, below the accounts of the setup assistant. Without the key, or when
  the gateway does not answer, one row "Beeper" says why. The answer is kept in memory for three minutes; addresses and phone
  numbers in it are never read. The key is masked in errors like `BEEPER_EXEC_KEY`. The page's texts no longer mention Cloudflare
  Access and say who changes which account. "Waiting by kind" and the tags on waiting requests name a Beeper request "Beeper"
  instead of "external", and a request of a type the server no longer has "Other"; `/dashboard/api/state` counts them as
  `external:<badge>`.

- **One set of tools for chats on every channel (fork). The old names are gone.** `messages_list_chats` (filters `account`, `type`,
  `unread`, `query`, `since`, `limit`), `messages_read_chat` (`chat_id`, `limit`, `cursor`), `messages_search` (`query`, `account`,
  `chat_id`, `since`, `before`, `limit`) and `messages_send_request` (`text`, `chat_id`, `handle`, `reply_to`, `request_id`)
  replace `imessage_list_chats`, `imessage_read_chat`, `imessage_search_messages`, `imessage_send_message` and
  `beeper_send_request`. A chat id starts with its channel, `imessage:…` or `beeper:…`; an id without a known prefix is refused,
  and paging is a `cursor` taken from `next_cursor`. Clients and skills that call the old names must change them (table below).
  The tools exist under the same settings as before: `ENABLE_IMESSAGE` for the three that read, `IMESSAGE_ALLOW_SEND` (with
  `ENABLE_IMESSAGE`) and `BEEPER_GATEWAY_URL` with `BEEPER_EXEC_KEY` for the one that writes, which is there when either is on.
  The `IMESSAGE_*` and `BEEPER_*` settings steer them as they steered the old tools (hidden chats, "only these chats", maximum age,
  service senders, the label on your own assistant's thread, the send lists), and the hub applies the reading lists to every
  channel, so they will hold for one added later. `messages_send_request` sends by the prefix of `chat_id`: `imessage:` goes to the
  iMessage approval, `beeper:` to the Beeper approval. Both keep their queue files, cards, hashes, statuses, log lines and push
  texts; a test compares what is stored with what the old tools stored. `handle` (a new iMessage conversation by address) is for
  iMessage only, and `chat_id` and `handle` must not come together. `reply_to` and `request_id` are for Beeper chats: an iMessage
  request that carries either is refused. The one line in the activity log that changes is the `tool.queued` line, which names the
  new tool. Titles are other people's text, like messages: they count towards `safety_warnings`. Archived chats are not listed
  yet, and Beeper chats cannot be read yet. Nothing is stored and reading is still not logged; a test checks that no title, chat id
  or text reaches the activity log or the server log. In `TOOLS`, `messages` is a new area and `imessage` stays valid and means the
  same tools; an old tool name in `TOOLS` is accepted with a warning and means the tool that replaced it. A `TOOLS=beeper` setting now
  matches no tool (the diagnostics stay), because the area had one tool and it is gone. The served instructions and the tool map
  name the new tools. `docs/DESIGN.md` has a Messages section.

  | Before | Now |
  | --- | --- |
  | `imessage_list_chats(query, limit, since, include_archived)` | `messages_list_chats(account, type, unread, query, since, limit)`; no archived chats yet |
  | `imessage_read_chat(chat_id, limit, before_id, since)` | `messages_read_chat(chat_id, limit, cursor)`; `chat_id` has the prefix; `before_id` is `cursor` (from `next_cursor`, was `older_before_id`); no `since` |
  | `imessage_search_messages(query, chat_id, limit, since, before)` | `messages_search(query, account, chat_id, since, before, limit)`; `chat_id` has the prefix |
  | `imessage_send_message(text, chat_id, handle)` | `messages_send_request(text, chat_id, handle)`; `chat_id` has the prefix `imessage:` |
  | `beeper_send_request(chat_id, text, reply_to, request_id)` | `messages_send_request(chat_id="beeper:<id>", text, reply_to, request_id)` |
  | In results: `name`, `group` | `title`, `type` (`single` or `group`) |
  | In results: `last_message_at`, `rowid`, `older_before_id`, `reply_to_rowid` | `last_at`, `message_id`, `next_cursor`, `reply_to` |
  | In results: `matches[].chat_name`, `matches[].group` | `matches[].chat_title`, `matches[].type` |
- **Beeper messages after your approval (fork).** With `BEEPER_GATEWAY_URL` and `BEEPER_EXEC_KEY` set, a new tool
  `messages_send_request` asks you to approve a message on any network behind Beeper, with `chat_id` as `beeper:` and the id Beeper gives. Nothing is sent when it is called. The card
  shows the chat as Beeper describes it (title, network, group or single), the server asks Beeper again just before it sends and
  stops when the chat changed, and the send goes through the gateway with a key of its own, so the server holds no Beeper token.
  `failed` (the gateway never took the call) puts the request back; anything after the call left is `unclear` and never repeated.
  The chats it may reach are a list of ids, `BEEPER_SEND_ALLOWLIST` (empty means nobody, `*` any chat), editable on the dashboard.
  A `*` in the owner's list of a type now allows every recipient; each server builds its own copy of the action registry. Off
  unless both settings are present, and then nothing else about the server changes.
- **Approvals can carry more kinds of action (fork, preparation only).** `actions.py` holds a registry of action types: how a
  request looks on the card, its parameters as a JSON Schema (checked with `jsonschema`, which `mcp` already requires and is now
  listed as a dependency), which parameter is the text the owner may edit, whom it reaches, and an executor that does the work.
  `external.py` turns the registered types into one more approval source, `external`, with its own queue file
  (`external-outbox.json`). The dashboard, editing, discarding, expiry, `approval_status` and `approval_withdraw` handle it with the
  code they already have; `approval_status` records of these requests add `action_type`, and the log lines carry it too. A
  request is only handed on when the owner approves: `sent` is done, `failed` (the other side never got it) puts it back in the
  queue, `unclear` (or an executor that raises) is never repeated and reads `unclear`. Whom a request may reach is the owner's
  list, checked when it is queued and again when it is approved. Nothing registers a type yet and nothing outside the tests
  calls `queue_external`, so a server without a registered type is unchanged: same tools, routes, pages, queue files and log
  lines, which `tests/test_hub_golden.py` pins for mail, calendar and iMessage. Not in this change: an HTTP input, a tool for
  agents, webhooks, `account` on a request, a reason or `changes_requested` on the card. `docs/DESIGN.md` has a Hub section.
- **The accounts file can hold several accounts per area (fork, preparation only).** `accounts.json` is now version 2: every
  account has a random id and a sign-in kind (`auth`, `app_password`; `oauth` is reserved and does nothing), and `defaults` names
  the account each area runs with. A file from before is read as version 2 in memory and rewritten only when you save on /setup;
  no copy is made, and the server runs with exactly the settings it had. Older servers still read a version 2 file. /setup still
  offers one account per area. Saving refuses a model with duplicate ids or a default that does not hold its area. The dashboard's
  account list says which account is the default of an area, but only where an area has more than one. `settings_for_account`
  builds the settings of one account by id for the optional `account` argument a later change will add; no tool uses it yet.
  Not in this change: that argument, the outbox, approval, OAuth, a second mailbox in use. `docs/DESIGN.md` says how they would attach.
- **An agent can take back its own waiting request (fork).** `approval_withdraw(id, reason)` removes a request that still waits,
  for mail, iMessage and calendar alike, by claiming it from its queue the way a discard does, so it and an Approve on the
  dashboard cannot both win. `reason` is `mistake`, `duplicate`, `superseded` or `no_longer_needed` (the default), never free text.
  Success answers `{id, withdrawn: true, status: "withdrawn"}`. A request that was already approved, discarded, expired or unknown
  answers `{id, withdrawn: false, status}` with the status `approval_status` gives it, and never anything that reads as success.
  The log gets `approval.withdrawn` by the actor `assistant` with `kind`, `outbox_id`, `reason` and the title, no text, and
  no second `tool.*` line. `approval_status` gains the status `withdrawn` (also in `since`); `edited` reads as before. Nothing
  is pushed and there is no rate limit yet. The tool stays with `TOOLS` presets and `essential`, refuses while the server is
  paused. Like `approval_status`, its name is an exception to `area_verb_noun`.
  A queued request now carries `requested_by`, the name the queueing client gave itself in its handshake or, on a stateless server (the default, which keeps no handshake), the name its app registered under (empty when there is neither),
  and only that client may take it back; an entry with no label can be taken back by any client. The label guards against mix-ups
  between two assistants. It is not a security boundary: a client cannot prove who it is. Home and the request show it as "from …".
  Home mentions for 24 hours that your assistant took requests back, as a quiet line that goes away by itself, and Activity
  shows "Your assistant withdrew …" with a "Withdrawn" tag, the reason in words and no text; the Approvals filter counts it.
- **Tool texts no longer say the owner password is the only way to approve (fork).** The queued-mail notice and the rules agents
  read now say that only the owner approves or discards, on the dashboard with their own sign-in (the password is the
  fallback), and how to take back a mistake: `approval_withdraw`, then ask the owner before queueing a corrected version or the
  same text again. The cap on the served instructions went from 10,500 to 10,800 characters and the tool list budget from
  182,500 to 185,300 bytes for it. Not in this change: several versions of one request, `replaces` and `changes_requested`.
- **`approval_status` says whether the owner edited the text (fork).** `edited` was always false; it is now true when the owner
  changed the text of a request and has not brought the agent's text back. A waiting request is edited when its queue entry
  still holds the text the agent asked for; one that has ended is edited when the newest of its `approval.edited` and
  `approval.edit_undone` log entries is the first, whether it was sent, discarded, expired or unclear. `unknown` stays false.
  The `since` list shows it the same way. Edit entries still end nothing and never move the cursor, and the answer holds
  no text. Typing the agent's wording back by hand now counts as bringing it back (`approval.edit_undone` in the log, as
  "Bring it back" already did), so a waiting and a sent answer agree.
- **A new look, and edit before you send (fork).** The dashboard follows the system's light or dark with one accent and bold
  sans titles. Today is now Home, with the count waiting by kind and a pause button in the side list on wide screens, and
  Settings brings its own list there. A request shows the message as it goes out, a warning for a group conversation, and
  how a send went: sending, sent, not sure (Messages did not confirm it; nothing is sent again) or not sent and still
  waiting. "Edit text" changes only the words of a mail (without an HTML part), an iMessage or an event's description
  (`PUT /dashboard/api/items/{key}/text`); the text the agent asked for stays under it, and "Bring it back" restores it.
  The log records that a text was edited, never the text. Settings lists the connected accounts, read only, and links to
  `/setup`.
- **An agent can ask what became of a request it queued (fork).** `approval_status` takes the `outbox_id` from a
  `queued_for_owner_approval` result and answers `waiting`, `sent`, `discarded`, `expired`, `unclear` or `unknown`, with the kind
  and a time and never the text, subject, names or recipients. An agent that heard nothing no longer has to choose between
  waiting and sending a second copy. With `since` it lists what was decided after a cursor, which is the hash of an entry in the
  activity log (`now` starts one). `unclear` means it may have gone out: Messages did not confirm an iMessage, or the release
  failed after the entry had left the queue. `unknown` means the id is neither waiting nor in the log (too old, never queued
  here, or `AUDIT_LOG=false`). The tool stays with the `TOOLS` presets and answers while the server is paused, and the tools
  that queue point to it. Its name is the one exception to `area_verb_noun`. Three changes to the activity log carry it:
  approval entries (`approval.sent`, `approval.discarded`, `approval.failed`, from the dashboard and from `/outbox`) now hold the
  `outbox_id`; an entry that runs out of time unanswered writes `approval.expired` instead of vanishing, also when it ran out
  while the server was down; and an iMessage that Messages did not confirm or reported as not delivered is marked with
  `delivery` on its `approval.sent` entry. `/outbox` now also logs a failed release as `approval.failed`.
- **The dashboard is an app (fork).** `/dashboard` is now an Ionic Core app with three tabs, Today, Activity and Settings,
  that talks to a JSON API under `/dashboard/api`. A request opens with its message, its attachments in a preview and,
  when a file is missing, the place to add it. Settings shows each area once, splits iCloud from the areas that work
  through the Mac and says whether the Mac helper is online. The old pages are gone; their addresses lead into the app,
  and tools now link one request as `approve_at`: `/dashboard#/item/<kind>:<id>`. Ionic is vendored under
  `static/vendor`; the rules for the look are in `docs/DESIGN.md`.
- **Push notes (fork).** A device can get a note when something waits for approval or a waiting mail got its file. The
  note says only what kind of item waits and is encrypted for the device (RFC 8291) and signed with the server's own
  VAPID key, both in `DATA_DIR/push.json`.

- **A mail can wait for a file.** An attachment with only a `filename` and no `content_base64` makes the mail wait for that
  file, also with `SEND_REQUIRES_APPROVAL=false`. The tool result gives the owner a link to `/dashboard/attach/<id>`, where
  they drop or choose the file and send the mail, and gives the agent an `upload_url` per file, so an agent with a shell can
  send the raw bytes with `curl -T`. The file goes into the stored message, which gets a new hash: the owner approves what
  actually goes out. `PUT /files/<token>` sits outside `/dashboard`, so a sign-in proxy in front of the server has to let
  that path through; the token is its only key. Needs the dashboard. A client that opens links itself (URL elicitation,
  MCP 2026-07-28) shows the owner the attach page directly; its retry gets the first answer and queues nothing twice. Until
  the mail goes out, the owner can replace an added file there or remove it, which puts the empty slot back and locks Send
  again. The agent's upload link stays used up, so only the owner adds the next file.
  `FILE_TOKEN_PREFIX` starts every upload link with a name of your choice (`/files/<prefix>.<token>`), so one proxy in
  front of several of these servers can send each upload to the right one by its path.
- **One interface for the owner.** With the dashboard on (the default) every `/outbox` address redirects to
  `/dashboard/approvals`, and tools return that page as `approve_at`. Approving now always follows the dashboard's sign-in
  rules: with `OWNER_PASSWORD_LOGIN=false` the owner password no longer releases messages on `/outbox`. The page that
  approves a new app connection uses the dashboard's look, and the site root links the dashboard. With `DASHBOARD=false`
  `/outbox` and the old pages stay as they were.
- **Mac helper 0.8.1: iMessage to a conversation Messages no longer shows.** Messages scripts only the conversations it
  currently shows, while chat.db keeps all of them, so a send to an older 1:1 conversation (and
  `--selftest-imessage-send` to your own address) failed with 'Messages does not know that conversation or handle'. For
  a 1:1 conversation the helper now falls back to its one participant over iMessage; a group never does.
- **SMS-only conversations are refused before they are queued.** `imessage_send_message` used to queue a message to a
  conversation that exists only as SMS, and the Mac refused it only after the owner approved it. Now the tool refuses at
  once and names the same person's 1:1 iMessage conversation that the allowlist permits, so the agent can send there.
- **A refresh sent twice keeps the app signed in.** A client that sends the same refresh token again within
  `REFRESH_TOKEN_GRACE_SECONDS` (default 60, 0 = off) gets the pair it was just exchanged for instead of `invalid_grant`,
  only while that pair is valid. Parallel refreshes and retries after a lost response no longer sign an app out. Kept in
  memory only.
- **The owner's dashboard.** `/dashboard` shows what is waiting for approval, what agents changed, the connected apps and
  whether each service answers, on a phone or a computer. It signs in with a passkey (WebAuthn, optional extra `[passkey]`,
  in the Docker image) or the owner password, which `OWNER_PASSWORD_LOGIN=false` turns off once a passkey exists. Sessions
  are stored as hashes; every form carries a CSRF token and must come from the server's own address. `/outbox` still works.
- **Activity log.** Writes by agents, approvals, sign-ins, connected and signed-out apps are recorded in
  `DATA_DIR/audit.jsonl` (names, addresses and subjects, no message bodies, hash-chained, `AUDIT_KEEP_DAYS`, `AUDIT_LOG`).
- **Calendar invitations can wait for approval.** `ALLOW_CALENDAR_INVITES=approve` holds every calendar change that would
  make iCloud email other people on the dashboard. Approving writes exactly the held object with `If-Match` (or
  `If-None-Match` for a new event), so an event changed in the meantime is never overwritten.

- **The dashboard updates itself and installs as an app.** Overview and Approvals pick up new requests within 15 seconds;
  Send and Discard work without reloading, a request can be opened and approved on the Overview, and a discard can be undone
  for 5 seconds; the Overview's separate "Open Approvals" button is gone. Polling does not extend the session. A manifest and a service worker that caches nothing make it
  installable; the stylesheet moved to `static/dashboard.css`.

- **Sign-in noise: summarized, and the owner's passkey sign-in stays.** Failed sign-ins held back by the lock are counted
  and written as one summary line when the ten-minute window ends, even if nothing follows. Sign-in tickets are signed with
  a per-process key instead of being stored, count as used only after a sign-in with the passkey succeeds, and are voided by
  a restart, so challenge floods cannot fill anything or evict the owner's sign-in.

- **Settings in the browser.** The dashboard lists every setting with where its value comes from and can change what the
  server does (areas, mail, invitations, contacts, the Mac helper, read-only, the dashboard). Values go to
  `DATA_DIR/settings.json`, are checked before saving and apply after a restart; weakening protection asks for the owner again.
  Addresses, ports, OAuth redirect hosts, tokens and connections stay in the environment. `DASHBOARD_SETTINGS=view|off`.
- **Optional owner sign-in through Cloudflare Access on `/outbox`.** Behind an Access application that already signs the
  owner in (for example a passkey through an OIDC provider), the outbox no longer asks for the owner password a second
  time. Set `OWNER_ACCESS_TEAM_DOMAIN`, `OWNER_ACCESS_AUD` and `OWNER_ACCESS_EMAILS`; the page then verifies the signed
  `Cf-Access-Jwt-Assertion` token (RS256 against the team keys, audience, issuer, expiry, email) and shows the queue.
  Without a valid token the password form appears as before. Off unless all three are set; `pyjwt[crypto]` is now a
  direct dependency (it was already installed through `mcp`).

## 0.12.1

- **Agents are told which tool is for which job.** The server's instructions now open with a short WHICH TOOL map: one line
  per goal (find mail, write mail, file mail, people, calendar, reminders, notes, files, messages, health...) naming the tool
  for each step. It is built from the tools actually registered, so `TOOLS=essential`, a read-only server or an area that is
  off never points an agent at a tool it does not have. Tests check that every tool is in the map and that the instructions
  name only real tools. Three routing hints the map now covers were shortened in the rules.

## 0.12.0

- **Breaking: tool names.** Every tool is now `<area>_<verb>_<noun>`. The 36 names that had a verb alone are renamed below.
  `TOOLS` still accepts the old names (with a warning). Clients see the new names after reconnecting; update any prompt,
  allow list or deny list that names a tool.

  | Old | New | Old | New |
  |---|---|---|---|
  | `mail_search` | `mail_search_messages` | `reminders_list` | `reminders_list_reminders` |
  | `mail_send` | `mail_send_message` | `reminders_create` | `reminders_create_reminder` |
  | `mail_reply` | `mail_reply_to_message` | `reminders_update` | `reminders_update_reminder` |
  | `mail_forward` | `mail_forward_message` | `reminders_complete` | `reminders_complete_reminder` |
  | `mail_mark` | `mail_mark_messages` | `reminders_move` | `reminders_move_reminder` |
  | `mail_move` | `mail_move_messages` | `reminders_delete` | `reminders_delete_reminder` |
  | `mail_delete` | `mail_delete_messages` | `notes_list` | `notes_list_notes` |
  | `mail_unsubscribe` | `mail_unsubscribe_from_list` | `notes_read` | `notes_read_note` |
  | `calendar_rsvp` | `calendar_respond_to_event` | `notes_create` | `notes_create_note` |
  | `contacts_search` | `contacts_search_contacts` | `notes_update` | `notes_update_note` |
  | `contacts_get` | `contacts_get_contact` | `notes_append` | `notes_append_to_note` |
  | `contacts_create` | `contacts_create_contact` | `notes_move` | `notes_move_note` |
  | `contacts_update` | `contacts_update_contact` | `notes_delete` | `notes_delete_note` |
  | `contacts_delete` | `contacts_delete_contact` | `drive_list` | `drive_list_folder` |
  | `shortcuts_list` | `shortcuts_list_shortcuts` | `drive_search` | `drive_search_files` |
  | `shortcuts_run` | `shortcuts_run_shortcut` | `drive_read` | `drive_read_file` |
  | `drive_move` | `drive_move_item` | `drive_write` | `drive_write_file` |
  | `drive_trash` | `drive_trash_item` | `health_refresh` | `health_refresh_data` |

  Mac helper operation names are unchanged.
- **Apple Health** (`ENABLE_HEALTH`, off by default; Mac helper 0.7.0): daily figures, one day in detail, and freshness from
  exports the owner's iPhone writes with a Shortcut, kept in a private store on the Mac, plus a one-off import of the Health
  app's full export. Overlapping exports never count twice; a day without data is reported as missing, not as zero.
- **ChatGPT can sign in by default:** `OAUTH_ALLOWED_REDIRECT_HOSTS` now also lists `chatgpt.com` and `chat.openai.com`, and
  their origins are accepted, next to Claude and local clients such as Codex. Every sign-in still needs the owner password;
  a deployment that sets the variable itself keeps its own list.
- **Hidden text in mail:** Outlook's styling no longer sets off the warning; it appears only when the hidden part reads like
  instructions to an assistant. `mail_get_message(show_hidden=true)` shows what was hidden, marked as untrusted.

## 0.11.0

- **Injection hardening** (#71): an invitation allowlist and attendee cap (`INVITE_ALLOWLIST`, `MAX_ATTENDEES`); text hidden
  in HTML mail removed with a warning; subjects, display names and listings screened, not only bodies; an optional
  classifier of your own (`SAFETY_SCREEN=command:<path>`); agent-added contact addresses journaled and marked
  (`CONTACTS_ALLOW_EMAIL_CHANGES`); `MAIL_MAX_AGE_DAYS`; the agent cannot rewrite its own notes file in iCloud Drive.
- **iCloud MCP Control**, a menu bar app for the Mac (`menubar/`): status, health, pause, restart and stop, owner passcode,
  iCloud app-specific password, and the connected apps (which ones, when each was last used, sign out one or all).
- Admin API for it, off unless `ADMIN_PORT` is set: loopback only, on its own port, token in `DATA_DIR/admin-token`.
- Pause: while `DATA_DIR/paused` exists every tool except the diagnostics answers that the server is paused.
- A passcode or app-specific password changed through the admin API is stored in `DATA_DIR/overrides.json` and takes
  precedence over the environment.
- The approval page has a "Discard all" button per queue. It clears only the messages shown on the page, so one queued later
  stays. An exact repeat of a waiting message is queued once, and a full queue tells the agent not to retry.
- Safety warnings on a stranger's text in more places: unsubscribe results, the sample in a bulk-action preview, and the
  subjects shown before deleting a mail folder.
- A calendar event with an unreadable date (a malformed invitation) is left out of listings instead of failing them. The booking
  extractor keeps the other events of an attachment when one is broken.
- Fuzz tests for the parsers that read other people's data: contact cards, invitations and repeat rules, booking data,
  List-Unsubscribe headers and the Messages decoder.

## 0.10.0

- Apple Maps (`ENABLE_MAPS`, Mac helper 0.6.0): `maps_get_travel_time` (walking, cycling, driving, public transport; for a
  departure or arrival time) and `maps_search_places`. With Maps on, calendar travel time uses a measured value first, else a
  labelled Apple Maps estimate.
- iMessage (`ENABLE_IMESSAGE`): `imessage_list_chats`, `imessage_read_chat`, `imessage_search_messages` over your own Messages
  history (the whole history unless `IMESSAGE_MAX_AGE_DAYS` limits it; `IMESSAGE_HIDDEN_CHATS` hides chats; service senders
  such as banks and one-time codes are hidden unless `IMESSAGE_HIDE_SHORT_CODES=false`; handles match however they are
  written), and a "Catch up on my messages" prompt.
- iMessage sending (`imessage_send_message`), off by default: owner approval on `/outbox` by default (independent of mail),
  an allowlist that starts empty, and a never-send list on the server and on the Mac itself. iMessage only; each send confirmed.

## 0.9.0

- Saved drafts: `mail_send_draft` sends a draft exactly as saved (same checks and approval as `mail_send`; the draft then goes to
  Trash), `mail_update_draft` changes one without losing it.
- Mail folders: `mail_update_folder` renames, `mail_delete_folder` deletes; a folder's mail always goes to Trash first, after a
  confirm step. The mailbox's own folders are refused.
- Calendars: `calendar_create_calendar`, `calendar_update_calendar` (rename), `calendar_delete_calendar` (never the default
  calendar; one with events only after a preview and its token).
- Contact groups, in the format the Contacts app uses: list, read, create, rename, add and remove members, delete (only the
  group, never its members). A contact shows the groups it is in. Fixed: a group card was listed as if it were a person.
- Reminders (Mac helper 0.5.0): repeating reminders, extra alerts, listing completed reminders with when they were done, and
  creating, renaming and deleting lists (deleting a list with reminders needs a preview and its token: Reminders has no trash).
- A server call that needs a newer Mac helper says so and asks for the update, instead of failing with "unknown operation".

## 0.8.0

- **Calendar reads cannot be stalled by a stranger's invitation:** a series that would repeat more than 48 times a day
  (every second or minute, or an hourly or daily rule multiplied up with BYMINUTE / BYSECOND lists) is no longer expanded.
  The calendar library expanded such rules before our guard saw them, so one invitation could hang a read until the tool
  timed out. Only the series' dated exceptions come through; the result counts the skipped series in `series_not_expanded`
  and never repeats their text. New and updated events refuse such rules too.
- **Stale message numbers are always caught on destructive mail actions:** `uidvalidity` is now required on `mail_delete`,
  `mail_move` and `mail_mark`. Breaking: a call without it is refused, and the agent retries with the value every search,
  thread and changes result carries.
- **Forwarded state is back:** mail results say `forwarded` again (lost in 0.6.0 when results became leaner).
- The Mac's hostname is no longer shown by `icloud_get_helper_status`.
- Mac helper 0.4.2: note backups older than 30 days move to the Trash once a day (never deleted outright).
- The sign-in and outbox pages follow the system light or dark mode.
- A failing integration test now fails the build; `CHANGELOG.md` and `CONTRIBUTING.md` added.

## 0.7.0

- Every tool name follows `area_verb_noun`; eleven tools renamed (`mail_changes` became `mail_list_changes`, `icloud_now`
  became `icloud_get_time`, and so on). `TOOLS` still accepts the old names.
- `mail_send` and `mail_reply` explain attachments; the instructions say which areas need the Mac helper.
- Mac helper 0.4.1.

## 0.6.0

- Calendar about four times faster on real iCloud (warm connections, all calendars read at once); mail reads fetch text
  only; sign-in at start (`WARMUP_ON_START`).
- Instructions built from the enabled tools; your own rules with `AGENT_NOTES_FILE`; `icloud_get_time` (then `icloud_now`),
  replies owed, clash and duplicate checks, invitations still to answer.
- One safe retry on a dropped connection, never after a write; timeouts name the slow step. Mac helper 0.4.0.

## 0.5.0

- Security hardening from a full audit (read-only mode, IMAP search text, invitations on recurring events, no duplicate
  events on retry, contact card fields, scrubbed errors). `reminders_move`.

## 0.4.2

- Privacy release.

## 0.4.1

- `calendar_move_event`; a privacy check on every pull request.

## 0.4.0

- Reused mail connections, what-changed tokens, newsletter radar, safe bulk clean-up with undo, safe unsubscribe, exact
  bookings, editable notes, search inside Drive files, birthdays, Shortcuts.

## 0.3.0

- One-click Claude Desktop bundle, free time, RSVP, single dates of a series, invitation delivery reports, all-folder
  search, the health check.

## 0.2.0

- Local mode for Claude Desktop and Claude Code, postal addresses, reading many messages at once, PyPI.
