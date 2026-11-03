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

Calm and light, after the Elune concept (newminds.studio/concept-elune), in fog grey.

| Token | Value | Used for |
| --- | --- | --- |
| `--ground` | `#f3f4f5` | page |
| `--surface` | `#ffffff` | lists, the message card |
| `--text` | `#1d2226` | all running text |
| `--muted` | `#5d666e` | sublines, notes |
| `--fog` (Ionic primary) | `#4f5d6a` | the one filled button, links, selection |
| `--positive` / `--negative` | `#247b50` / `#b3263a` | On, Remove, warnings |
| `--serif` | system New York | each screen's title only |

- One serif title per screen (`.title h1`, 30px). Section heads and everything else use the system sans.
- No small uppercase labels above titles, no icons in tinted boxes, no shadows on things at rest. The tab bar is fixed at
  the bottom, and its active tab is grey, not coloured.
- A push note says only what kind of item waits, never who it goes to or what it says.

## Screens

| Screen | Element | Content |
| --- | --- | --- |
| Today | `icm-today` | Greeting by time of day, one sentence on what waits and when the oldest is dropped, the live line, notices (result, paused, burst, install tip), Waiting, Sent today |
| One request | `icm-item` | Title, one line on who and when, the warning with the mail it answers, missing files first, the message as it goes out, attachments (tap for preview), and Discard plus the approve button. When there is a warning, Discard is the main button and "Send anyway…" asks first |
| Activity | `icm-activity` | Filter segment, entries per day in plain words, chain check and download |
| Settings | `icm-settings` | Sign-in and security, iCloud, Your Mac (live state of the helper), safety switches, account and server |
| One area | `icm-group` | The area's fields as Ionic controls, Save in the toolbar, reset per field |
| Notifications, Passkeys, Connected apps, Owner password, Server | `icm-*` | One job each |
| Sign in / Confirm it is you | `app.js`, `ui.js` | Passkey first, then the owner password |

At 900px and wider, the tabs become a side list, and Today shows the chosen request beside the list.

## Own pieces

Ionic has nothing for these, so they are written here, each in one place in `app.css`:

- `.title`, `.fresh`, `.notice` and `.note`: text blocks
- `.mail`: the message exactly as it goes out
- `.warn`: why a reply was held
- `.slot`: the drop area for a missing file
- `.split` and `.side`: the wide-screen layout

## Links into the app

- `#/item/<kind>:<id>` opens one request. Tools give this in `approve_at` (`Settings.approval_link`), and push notes open
  it too.
- `#/activity` and `#/settings` open those tabs.
- Old addresses (`/dashboard/approvals?item=`, `/dashboard/attach/<id>`, `/dashboard/security`, `/dashboard/settings`)
  redirect to them.
