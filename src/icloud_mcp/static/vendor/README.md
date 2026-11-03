# Vendored UI libraries

Served from this server only (the dashboard's CSP is `script-src 'self'`), pinned to one version each.

| Library | Version | Licence | Source | What is here |
| --- | --- | --- | --- | --- |
| Ionic Core | 9.0.10 | MIT | `npm pack @ionic/core@9.0.10` | `dist/ionic/` (lazy-loaded components), `css/core.css`, `structure.css`, `typography.css`, `normalize.css`; source maps and the bundled icon set removed |
| Ionicons | 8.1.0 | MIT | `npm pack ionicons@8.1.0` | the outline icons the dashboard uses, loaded through `<ion-icon src>` |

A version bump is its own change with a look at every dashboard screen on a phone and a desktop.
