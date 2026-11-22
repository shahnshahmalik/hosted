# Contributing

Thanks for helping! Issues and pull requests are welcome.

## Run it locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
LABCANVAS_DB=data/dev.db .venv/bin/uvicorn app.main:app --reload --port 8080
```

There is no frontend build step: edit `static/*.js` / `static/style.css` and reload the page.
Preact and htm are vendored in `static/vendor/`.

## Where things live

| Path | What |
|---|---|
| `app/main.py` | HTTP routes, widget frames, proxy, chat endpoint |
| `app/store.py` | SQLite data layer, manifests, themes, permissions |
| `app/proxy.py` | outbound proxy that enforces widget grants |
| `app/sdk.js` | the `hosted` SDK injected into every widget frame |
| `app/ai.py`, `app/ai_tools.py`, `app/prompt.md` | AI assistant: providers, tools, system prompt |
| `app/ai_edit.py`, `app/prompt_edit.md`, `static/editor.js` | widget editor: AI edits a draft, preview, save |
| `app/prompt_widgets.md` | widget-writing guide shared by both assistants |
| `app/plugins.py` | installing widgets from GitHub |
| `app/system.py` | host stats and URL checks for system widgets |
| `widgets/<id>/` | built-in widgets (`manifest.json` + `index.html`) |
| `themes/*.json` | built-in themes (design tokens) |
| `defaults/dashboard.json` | what a fresh install starts with |

## Guidelines

- Keep it Pi-friendly: no build step, few dependencies, modest CPU and memory use.
- Style only with theme tokens; never hard-code colors (see [themes/README.md](themes/README.md)).
- Widgets must stay inside the sandbox model: new abilities go through declared permissions or
  capabilities the user approves (see [WIDGETS.md](WIDGETS.md)).
- Contributing a widget? Built-in widgets live in `widgets/`; third-party ones can be published
  in their own repo and installed from GitHub.
- Please describe how you tested your change in the pull request.
