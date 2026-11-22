# Hosted

A self-hosted dashboard whose widgets, layouts and themes you can create by chatting with an AI.
Runs on a Raspberry Pi 4 or anything with Python 3.12 or Docker. No build step.

- 14 built-in widgets: clock, weather, notes, countdown, exchange rates, crypto prices, Hacker News,
  GitHub repo, services (homepage-style tiles with status), system info, processor, memory, disks,
  CPU temperature
- Every widget runs in a sandboxed iframe and can only reach hosts, secrets and server
  capabilities you approve
- AI assistant (bring your own Anthropic, OpenAI or Ollama key) that writes widgets, arranges the
  layout and changes the theme
- Themes as token files in `themes/` (Midnight, Paper, Ember, Neon Grid)
- Install widgets from GitHub, or download any widget as a `.zip` to publish ([WIDGETS.md](WIDGETS.md))

## Run with Docker

```bash
docker build -t hosted .
docker run -d --name hosted -p 8080:8080 -v hosted-data:/data hosted
```

Open http://localhost:8080.

### Host stats in Docker

The system widgets (Processor, Memory, CPU temperature, Disks, System info) read the machine's
stats. Inside a container, CPU, memory and temperature already come from the host. To also see
the host's drives and name, mount them read-only and tell Hosted which paths to show:

```bash
docker run -d --name hosted -p 8080:8080 -v hosted-data:/data \
  --hostname "$(hostname)" \
  -v /:/hostfs:ro -v /mnt/usb:/hostfs-usb:ro \
  -e HOSTED_DISK_PATHS=/hostfs,/hostfs-usb \
  hosted
```

## Run without Docker

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Data lives in `data/canvas.db` (override with `HOSTED_DB=/path/to.db`).

## Security notes

Hosted has no login yet: anyone who can reach it can change the dashboard and use the configured
AI key. Keep it on your LAN or behind a reverse proxy with authentication. Details of the widget
sandbox are in [WIDGETS.md](WIDGETS.md#security-model).
