# Changelog

## 0.2.0

- Renamed from Hosted to **LabCanvas** (repo, Docker image `ghcr.io/shahnshahmalik/labcanvas`,
  `LABCANVAS_*` environment variables; the old `HOSTED_*` names still work).
  Existing installs keep working: point the new image at your existing data volume
  (e.g. `-v hosted-data:/data`) to keep your dashboard.
- Widget editor: a ✎ button on every widget opens its code (70%) next to an AI chat (30%). The AI
  edits the draft with exact find/replace edits, you preview it in the real sandbox and save.
  Built-in and installed widgets are copied on first save; drafts survive a page reload.

## 0.1.1

- Fix: after upgrading, browsers could keep running the previous version's scripts and styles
  (for example system widgets showing "unknown call system"). Scripts and stylesheets are now
  loaded with a content-hash version, cached long-term, and always refreshed after an upgrade.

## 0.1.0 (first public release)

- 12-column drag-and-resize grid with persistence in SQLite
- Sandboxed widget runtime: opaque-origin iframes, strict CSP, `hosted` SDK over postMessage
- Outbound proxy with per-widget host, secret and capability grants; write-only secrets
- AI assistant (Anthropic, OpenAI, Ollama) that creates widgets, arranges the layout and sets themes
- 14 built-in widgets, including live system stats and a homepage-style Services widget
- Themes as token files: Neon Grid (default), Midnight, Paper, Ember; bundled fonts
- Install widgets from GitHub (pinned to a commit, with update checks) and export any widget as a zip
- Docker image for amd64 and arm64 (Raspberry Pi 4/5)
