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
| Home | `icm-today` | Greeting by time of day, one sentence on what waits and when the oldest is dropped, the live line, notices (result, paused, burst, install tip), Waiting (kind tag, group tag, time left), Sent today |
| One request | `icm-item` | Title, one line on who asked and when (and "Edited by you"), the warning, the mail it answers, missing files first, the message as it goes out with "Edit text", the text the agent asked for with "Bring it back" when it was edited, attachments (tap for preview), and Discard plus the approve button. When there is a warning, Discard is the main button and "Send anyway…" asks first |
| After Approve | `icm-item` | Sending (the buttons are off), sent (back to Home with the result), unclear (Messages did not confirm it: nothing is sent again, the page stays until Done), not sent (the request still waits: Try again or Discard) |
| Edit text | `icm-item`, `edit.js` | The message card with its text as a text field, a character count, Cancel and Save changes. Only the text changes. A mail with an HTML part cannot be edited and says why |
| Activity | `icm-activity` | Filter segment, entries per day in plain words with a tag where one helps (Edited, Unclear), chain check and download |
| Settings | `icm-settings` | One group per section: sign-in and security, connected accounts, iCloud, your Mac (live state of the helper), safety switches, account and server. On a wide screen a list of the sections replaces the side list, with "Back to app" |
| Connected accounts | `icm-accounts` | The accounts the setup assistant saved (provider, address, areas, read only), read only, with a link to `/setup` |
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
