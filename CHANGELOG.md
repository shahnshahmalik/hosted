# Changelog

## 0.1.0 (first public release)

- 12-column drag-and-resize grid with persistence in SQLite
- Sandboxed widget runtime: opaque-origin iframes, strict CSP, `hosted` SDK over postMessage
- Outbound proxy with per-widget host, secret and capability grants; write-only secrets
- AI assistant (Anthropic, OpenAI, Ollama) that creates widgets, arranges the layout and sets themes
- 14 built-in widgets, including live system stats and a homepage-style Services widget
- Themes as token files: Neon Grid (default), Midnight, Paper, Ember; bundled fonts
- Install widgets from GitHub (pinned to a commit, with update checks) and export any widget as a zip
- Docker image for amd64 and arm64 (Raspberry Pi 4/5)
