# Security

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/shahnshahmalik/hosted/security/advisories/new)
instead. You can expect an acknowledgement within a week.

Especially welcome: ways for a widget to escape its sandbox, read another widget's data, reach
hosts or secrets it wasn't granted, or obtain the AI provider key.

## Deployment notes

- **Hosted has no login yet.** Anyone who can reach it can change the dashboard, read widget
  data and spend the configured AI key. Run it on a trusted network or behind a reverse proxy
  that adds authentication.
- Secrets and AI keys are stored unencrypted in the SQLite database (`/data` in Docker). Protect
  that volume like any credential store.
- The widget sandbox and its known limits are documented in
  [WIDGETS.md](WIDGETS.md#security-model).
