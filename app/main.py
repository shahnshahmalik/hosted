import html
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .proxy import ProxyError, proxy_request

DB_PATH = os.environ.get("HOSTED_DB") or os.environ.get("CANVAS_DB") or "data/canvas.db"
ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
WIDGETS_DIR = ROOT / "widgets"
SDK_JS = (Path(__file__).parent / "sdk.js").read_text()

SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
STORAGE_MAX_VALUE = 64 * 1024
STORAGE_MAX_KEYS = 100

app = FastAPI(title="Hosted")


def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def migrate():
    with closing(db()) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS widgets (
              id TEXT PRIMARY KEY, title TEXT NOT NULL,
              x INTEGER NOT NULL, y INTEGER NOT NULL, w INTEGER NOT NULL, h INTEGER NOT NULL,
              config TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS widget_defs (
              id TEXT PRIMARY KEY, manifest TEXT NOT NULL, code TEXT NOT NULL,
              builtin INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS widget_storage (
              wid TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (wid, key));
            """
        )
        cols = {r["name"] for r in c.execute("PRAGMA table_info(widgets)")}
        if "grants" not in cols:
            # Permissions the user approved when adding the instance. Not writable via PUT.
            c.execute("ALTER TABLE widgets ADD COLUMN grants TEXT NOT NULL DEFAULT '{\"hosts\":[],\"secrets\":[]}'")
        c.commit()


def seed_builtin_widgets():
    """Load widgets/<id>/{manifest.json,index.html} into widget_defs (builtins are overwritten)."""
    if not WIDGETS_DIR.is_dir():
        return
    with closing(db()) as c:
        for d in sorted(WIDGETS_DIR.iterdir()):
            mf, code = d / "manifest.json", d / "index.html"
            if not (mf.is_file() and code.is_file()):
                continue
            manifest = normalize_manifest(json.loads(mf.read_text()), d.name)
            c.execute(
                "INSERT INTO widget_defs (id, manifest, code, builtin) VALUES (?,?,?,1) "
                "ON CONFLICT(id) DO UPDATE SET manifest=excluded.manifest, code=excluded.code "
                "WHERE widget_defs.builtin=1",
                (d.name, json.dumps(manifest), code.read_text()),
            )
        c.commit()


def normalize_manifest(m: dict, wid: str) -> dict:
    perms = m.get("permissions") or {}
    secrets = [s for s in perms.get("secrets", []) if SECRET_NAME.match(s)]
    return {
        "id": wid,
        "name": m.get("name", wid),
        "description": m.get("description", ""),
        "size": {"w": int(m.get("size", {}).get("w", 3)), "h": int(m.get("size", {}).get("h", 2))},
        "permissions": {"hosts": [str(h).lower() for h in perms.get("hosts", [])], "secrets": secrets},
        "props": m.get("props") or {},
    }


migrate()
seed_builtin_widgets()


# ---------- widget definitions ----------

def get_def(c, def_id):
    r = c.execute("SELECT * FROM widget_defs WHERE id=?", (def_id,)).fetchone()
    return r and {"manifest": json.loads(r["manifest"]), "code": r["code"], "builtin": bool(r["builtin"])}


@app.get("/api/widget-defs")
def list_defs():
    with closing(db()) as c:
        return [
            {**json.loads(r["manifest"]), "builtin": bool(r["builtin"])}
            for r in c.execute("SELECT manifest, builtin FROM widget_defs ORDER BY id")
        ]


# ---------- widget instances ----------

class Widget(BaseModel):
    id: str | None = None
    title: str = "Widget"
    x: int = Field(0, ge=0)
    y: int = Field(0, ge=0)
    w: int = Field(3, ge=1, le=12)
    h: int = Field(2, ge=1, le=20)
    config: dict = {}


def row_to_widget(r):
    return {
        **{k: r[k] for k in ("id", "title", "x", "y", "w", "h")},
        "config": json.loads(r["config"]),
        "grants": json.loads(r["grants"]),
    }


def get_widget_row(c, wid):
    r = c.execute("SELECT * FROM widgets WHERE id=?", (wid,)).fetchone()
    if not r:
        raise HTTPException(404, "widget not found")
    return r


@app.get("/api/widgets")
def list_widgets():
    with closing(db()) as c:
        return [row_to_widget(r) for r in c.execute("SELECT * FROM widgets")]


@app.post("/api/widgets", status_code=201)
def create_widget(w: Widget):
    w.id = uuid.uuid4().hex[:12]
    grants = {"hosts": [], "secrets": []}
    with closing(db()) as c:
        if t := w.config.get("type"):
            d = get_def(c, t)
            if not d:
                raise HTTPException(400, f"unknown widget type {t}")
            # Adding the widget from the picker is the user's consent to its declared permissions.
            grants = d["manifest"]["permissions"]
        c.execute(
            "INSERT INTO widgets (id, title, x, y, w, h, config, grants) VALUES (?,?,?,?,?,?,?,?)",
            (w.id, w.title, w.x, w.y, w.w, w.h, json.dumps(w.config), json.dumps(grants)),
        )
        c.commit()
        return row_to_widget(get_widget_row(c, w.id))


@app.put("/api/widgets/{wid}")
def update_widget(wid: str, w: Widget):
    with closing(db()) as c:
        old = json.loads(get_widget_row(c, wid)["config"])
        if old.get("type") != w.config.get("type"):
            raise HTTPException(400, "widget type cannot be changed; add a new widget instead")
        c.execute(
            "UPDATE widgets SET title=?, x=?, y=?, w=?, h=?, config=? WHERE id=?",
            (w.title, w.x, w.y, w.w, w.h, json.dumps(w.config), wid),
        )
        c.commit()
        return row_to_widget(get_widget_row(c, wid))


@app.delete("/api/widgets/{wid}", status_code=204)
def delete_widget(wid: str):
    with closing(db()) as c:
        c.execute("DELETE FROM widgets WHERE id=?", (wid,))
        c.execute("DELETE FROM widget_storage WHERE wid=?", (wid,))
        c.commit()


def effective_grants(c, row) -> dict:
    """Granted ∩ currently declared: a redefined widget can never silently gain access."""
    granted = json.loads(row["grants"])
    t = json.loads(row["config"]).get("type")
    d = t and get_def(c, t)
    if not d:
        return {"hosts": [], "secrets": []}
    declared = d["manifest"]["permissions"]
    return {k: [v for v in granted.get(k, []) if v in declared.get(k, [])] for k in ("hosts", "secrets")}


# ---------- secrets (write-only) ----------

class SecretIn(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


@app.get("/api/secrets")
def list_secrets():
    with closing(db()) as c:
        return [r["name"] for r in c.execute("SELECT name FROM secrets ORDER BY name")]


@app.put("/api/secrets/{name}", status_code=204)
def set_secret(name: str, s: SecretIn):
    if not SECRET_NAME.match(name):
        raise HTTPException(400, "secret names are UPPER_SNAKE_CASE")
    with closing(db()) as c:
        c.execute("INSERT OR REPLACE INTO secrets VALUES (?,?)", (name, s.value))
        c.commit()


@app.delete("/api/secrets/{name}", status_code=204)
def delete_secret(name: str):
    with closing(db()) as c:
        c.execute("DELETE FROM secrets WHERE name=?", (name,))
        c.commit()


# ---------- per-widget storage ----------

class StorageIn(BaseModel):
    value: object = None


def check_key(key: str):
    if not (0 < len(key) <= 128):
        raise HTTPException(400, "storage key must be 1-128 chars")


@app.get("/api/widgets/{wid}/storage/{key:path}")
def storage_get(wid: str, key: str):
    check_key(key)
    with closing(db()) as c:
        r = c.execute("SELECT value FROM widget_storage WHERE wid=? AND key=?", (wid, key)).fetchone()
        return {"value": json.loads(r["value"]) if r else None}


@app.put("/api/widgets/{wid}/storage/{key:path}", status_code=204)
def storage_set(wid: str, key: str, body: StorageIn):
    check_key(key)
    value = json.dumps(body.value)
    if len(value) > STORAGE_MAX_VALUE:
        raise HTTPException(413, "storage value exceeds 64 KB")
    with closing(db()) as c:
        get_widget_row(c, wid)
        exists = c.execute("SELECT 1 FROM widget_storage WHERE wid=? AND key=?", (wid, key)).fetchone()
        count = c.execute("SELECT COUNT(*) FROM widget_storage WHERE wid=?", (wid,)).fetchone()[0]
        if not exists and count >= STORAGE_MAX_KEYS:
            raise HTTPException(413, f"widget storage is limited to {STORAGE_MAX_KEYS} keys")
        c.execute("INSERT OR REPLACE INTO widget_storage VALUES (?,?,?)", (wid, key, value))
        c.commit()


@app.delete("/api/widgets/{wid}/storage/{key:path}", status_code=204)
def storage_delete(wid: str, key: str):
    with closing(db()) as c:
        c.execute("DELETE FROM widget_storage WHERE wid=? AND key=?", (wid, key))
        c.commit()


# ---------- proxy ----------

@app.post("/api/widgets/{wid}/proxy")
async def proxy(wid: str, request: Request):
    req = await request.json()
    with closing(db()) as c:
        grants = effective_grants(c, get_widget_row(c, wid))
        names = grants["secrets"]
        values = {
            r["name"]: r["value"]
            for r in c.execute(
                f"SELECT name, value FROM secrets WHERE name IN ({','.join('?' * len(names))})", names
            )
        } if names else {}
    try:
        return await proxy_request(req, grants, values)
    except ProxyError as e:
        return JSONResponse({"detail": e.detail}, status_code=e.status)
    except Exception as e:  # network failures, timeouts, bad URLs
        return JSONResponse({"detail": f"upstream error: {type(e).__name__}: {e}"[:300]}, status_code=502)


# ---------- sandboxed frame ----------

FRAME_CSP = "; ".join([
    "default-src 'none'",
    "script-src 'unsafe-inline'",
    "style-src 'unsafe-inline'",
    "img-src data: blob:",
    "font-src data:",
    "media-src data: blob:",
    "connect-src 'none'",
    "frame-src 'none'",
    "form-action 'none'",
    "base-uri 'none'",
    "frame-ancestors 'self'",
])

FRAME_BASE_CSS = """
:root { color-scheme: dark; }
html, body { margin: 0; height: 100%; }
body { background: transparent; color: var(--text, #e6e8ee); font: 14px system-ui, sans-serif;
  padding: 0 12px 12px; box-sizing: border-box; overflow: auto; }
* { box-sizing: border-box; }
.muted { color: var(--muted, #8b90a0); }
.error { color: #ff7a7a; }
"""


@app.get("/frame/{wid}")
def frame(wid: str):
    with closing(db()) as c:
        row = get_widget_row(c, wid)
        t = json.loads(row["config"]).get("type")
        d = t and get_def(c, t)
    code = d["code"] if d else '<p class="muted">Empty widget</p>'
    doc = (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>{html.escape(t or 'widget')}</title>"
        f"<style>{FRAME_BASE_CSS}</style><script>{SDK_JS}</script></head>"
        f"<body>{code}</body></html>"
    )
    return HTMLResponse(doc, headers={
        "Content-Security-Policy": FRAME_CSP,
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    })


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/", StaticFiles(directory=STATIC), name="static")
