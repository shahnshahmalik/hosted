import asyncio
import hashlib
import html
import io
import json
import secrets as pysecrets
import time
import zipfile
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, ai, ai_edit, plugins, store, system
from .proxy import ProxyError, proxy_request
from .store import StoreError, db

STATIC = store.ROOT / "static"
SDK_JS = (Path(__file__).parent / "sdk.js").read_text()
FONTS_CSS = (STATIC / "fonts" / "fonts.css").read_text()
STORAGE_MAX_VALUE = 64 * 1024
STORAGE_MAX_KEYS = 100

app = FastAPI(title="LabCanvas", version=__version__)


@app.middleware("http")
async def static_headers(request: Request, call_next):
    """Caching for static files, and CORS for fonts.

    The page loads every script and stylesheet as "<path>?v=<ASSET_VERSION>", so those responses
    can be cached forever: an upgrade changes the version and browsers fetch the new files.
    Unversioned requests must revalidate, so a stale copy is never used without asking.
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith(("/api/", "/frame/", "/frame-preview/", "/edit/")) or path == "/":
        return response  # these set their own headers
    if path.startswith("/fonts/"):
        # Widget frames have an opaque origin and font loads are CORS requests.
        response.headers["Access-Control-Allow-Origin"] = "*"
    if request.query_params.get("v") == ASSET_VERSION:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.startswith("/fonts/") and path.endswith(".woff2"):
        response.headers["Cache-Control"] = "public, max-age=604800"
    else:
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(StoreError)
async def store_error(_, e: StoreError):
    status = 404 if "not found" in str(e) else 400
    return JSONResponse({"detail": str(e)}, status_code=status)


store.migrate()
store.seed_builtin_widgets()
store.seed_themes()
store.seed_default_dashboard()


# ---------- widget types ----------

@app.get("/api/widget-defs")
def list_defs():
    with closing(db()) as c:
        return store.list_defs(c)


@app.delete("/api/widget-defs/{type_id}", status_code=204)
def delete_def(type_id: str):
    """Uninstall a plugin or delete an AI-made type (builtins can't be deleted)."""
    with closing(db()) as c:
        d = store.get_def(c, type_id, with_code=False)
        if not d:
            raise StoreError(f"widget type {type_id!r} not found")
        if d["source"] == "builtin":
            raise StoreError("builtin widget types cannot be removed")
        store.delete_def(c, type_id, source=d["source"])


PACKAGE_README = """# {name}

{description}

A widget for LabCanvas, the self-hosted AI dashboard.

## Install

Push this folder to a GitHub repository, then in LabCanvas open **+ Add widget → Install from GitHub**
and paste the repository URL (or the URL of this folder, e.g.
`https://github.com/<you>/<repo>/tree/main/{id}`).

## Permissions

{permissions}

## Files

- `manifest.json`: name, size, settings (props) and the hosts/secrets the widget needs
- `index.html`: the widget itself (HTML/CSS/JS using the `hosted` SDK)
"""


@app.get("/api/widget-defs/{type_id}/export")
def export_def(type_id: str):
    """Download a widget type as a ready-to-publish package: <id>/manifest.json, index.html, README.md."""
    with closing(db()) as c:
        d = store.get_def(c, type_id)
    if not d:
        raise StoreError(f"widget type {type_id!r} not found")
    manifest = {"id": d["id"], "name": d["name"], "description": d["description"],
                "version": d.get("version") or "1.0.0", "author": d.get("author") or "",
                **({k: d[k] for k in ("homepage", "license") if d.get(k)}),
                "size": d["size"], "permissions": d["permissions"], "props": d["props"]}
    perms = d["permissions"]
    lines = [f"- Contacts `{h}`" for h in perms["hosts"]] + [f"- Uses the secret `{s}`" for s in perms["secrets"]] + \
        [f"- Can {store.CAPABILITIES[x]}" for x in perms.get("capabilities", [])]
    readme = PACKAGE_README.format(name=d["name"], description=d["description"] or "", id=d["id"],
                                   permissions="\n".join(lines) or "None: this widget has no network access.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{d['id']}/manifest.json", json.dumps(manifest, indent=2) + "\n")
        z.writestr(f"{d['id']}/index.html", d["code"])
        z.writestr(f"{d['id']}/README.md", readme)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{d["id"]}.zip"'})


# ---------- plugins from GitHub ----------

class PreviewIn(BaseModel):
    url: str = Field(max_length=500)


class InstallIn(BaseModel):
    url: str = Field(max_length=500)
    sha: str = Field(max_length=40)
    path: str = Field("", max_length=300)


@app.post("/api/plugins/preview")
async def plugin_preview(body: PreviewIn):
    return await plugins.preview(body.url)


@app.post("/api/plugins/install")
async def plugin_install(body: InstallIn):
    return await plugins.install(body.url, body.sha, body.path)


@app.get("/api/plugins/updates")
async def plugin_updates():
    return await plugins.check_updates()


@app.post("/api/plugins/{type_id}/update")
async def plugin_update(type_id: str):
    return await plugins.update(type_id)


# ---------- widget instances ----------

class Widget(BaseModel):
    id: str | None = None
    title: str = "Widget"
    x: int = Field(0, ge=0)
    y: int = Field(0, ge=0)
    w: int = Field(3, ge=1, le=12)
    h: int = Field(2, ge=1, le=20)
    config: dict = {}


@app.get("/api/widgets")
def list_widgets():
    with closing(db()) as c:
        return store.list_widgets(c)


@app.post("/api/widgets", status_code=201)
def create_widget(w: Widget):
    with closing(db()) as c:
        # Adding from the picker is the user's consent to the type's declared permissions.
        return store.create_widget(c, w.title, w.config, {"x": w.x, "y": w.y, "w": w.w, "h": w.h}, grant=True)


@app.put("/api/widgets/{wid}")
def update_widget(wid: str, w: Widget):
    with closing(db()) as c:
        # Layout overlap is enforced client-side during drag; the grid UI never sends overlaps.
        return store.update_widget(c, wid, w.title, {"x": w.x, "y": w.y, "w": w.w, "h": w.h}, w.config,
                                   check_overlap=False)


@app.post("/api/widgets/{wid}/grant")
def grant_widget(wid: str):
    """User clicked Allow. Not exposed to the AI."""
    with closing(db()) as c:
        return store.grant_widget(c, wid)


@app.delete("/api/widgets/{wid}", status_code=204)
def delete_widget(wid: str):
    with closing(db()) as c:
        store.delete_widget(c, wid)


# ---------- theme ----------

@app.get("/api/themes")
def list_themes():
    with closing(db()) as c:
        return store.list_themes(c)


@app.get("/api/theme")
def get_theme():
    """Active theme: {id, name, overrides, vars (effective tokens)}."""
    with closing(db()) as c:
        return store.active_theme(c)


class ThemeIn(BaseModel):
    id: str
    overrides: dict[str, str] = {}


@app.put("/api/theme")
def put_theme(t: ThemeIn):
    with closing(db()) as c:
        return store.set_active_theme(c, t.id, t.overrides)


# ---------- secrets (write-only) ----------

class SecretIn(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


@app.get("/api/secrets")
def list_secrets():
    with closing(db()) as c:
        return store.secret_names(c)


@app.put("/api/secrets/{name}", status_code=204)
def set_secret(name: str, s: SecretIn):
    if not store.SECRET_NAME.match(name):
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
        store.get_widget_row(c, wid)
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
        grants = store.effective_grants(c, store.get_widget_row(c, wid))
        names = grants["secrets"]
        values = {
            r["name"]: r["value"]
            for r in c.execute(f"SELECT name, value FROM secrets WHERE name IN ({','.join('?' * len(names))})", names)
        } if names else {}
    try:
        return await proxy_request(req, grants, values)
    except ProxyError as e:
        return JSONResponse({"detail": e.detail}, status_code=e.status)
    except Exception as e:  # network failures, timeouts, bad URLs
        return JSONResponse({"detail": f"upstream error: {type(e).__name__}: {e}"[:300]}, status_code=502)


# ---------- server capabilities for widgets ----------

def require_capability(c, wid, cap):
    if cap not in store.effective_grants(c, store.get_widget_row(c, wid)).get("capabilities", []):
        raise HTTPException(403, f"this widget has not been allowed to {store.CAPABILITIES[cap]}")


@app.get("/api/widgets/{wid}/system")
async def widget_system(wid: str):
    with closing(db()) as c:
        require_capability(c, wid, "system")
    return await asyncio.get_running_loop().run_in_executor(system.EXECUTOR, system.snapshot)


class PingIn(BaseModel):
    urls: list[str] = Field(max_length=40)


@app.post("/api/widgets/{wid}/ping")
async def widget_ping(wid: str, body: PingIn):
    with closing(db()) as c:
        require_capability(c, wid, "ping")
    urls = [u for u in body.urls if isinstance(u, str) and u.startswith(("http://", "https://")) and len(u) < 500]
    return await system.ping(urls)


# ---------- widget editor ----------

@app.get("/api/widgets/{wid}/source")
def widget_source(wid: str):
    with closing(db()) as c:
        row = store.get_widget_row(c, wid)
        w = store.row_to_widget(c, row)
        t = w["config"].get("type")
        d = store.get_def(c, t) if t else None
        users = sum(1 for x in store.list_widgets(c) if x["config"].get("type") == t) if t else 1
    manifest = store.editable_manifest(d) if d else {"name": w["title"], "size": {"w": w["w"], "h": w["h"]},
                                                    "permissions": dict(store.EMPTY_PERMS), "props": {}}
    return {"widget": w, "type": t, "source": d["source"] if d else None, "revision": d["revision"] if d else 0,
            "manifest": manifest, "code": d["code"] if d else "", "instances": users,
            # builtin/installed types are copied on save; AI-made ones change for every instance
            "copy_on_save": not d or d["source"] != "ai"}


class SourceIn(BaseModel):
    manifest: dict
    code: str = Field(max_length=store.MAX_CODE)


@app.put("/api/widgets/{wid}/source")
def save_widget_source(wid: str, body: SourceIn):
    with closing(db()) as c:
        return store.save_widget_source(c, wid, body.manifest, body.code)


class EditChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    history: list[dict] = []
    manifest: dict
    code: str = Field(max_length=store.MAX_CODE)
    errors: list[str] = []


@app.post("/api/widgets/{wid}/edit-chat")
async def widget_edit_chat(wid: str, body: EditChatIn):
    with closing(db()) as c:
        row = store.get_widget_row(c, wid)
    draft = {"manifest": body.manifest, "code": body.code, "type_id": json.loads(row["config"]).get("type")}

    async def events():
        async for ev in ai_edit.edit_chat(wid, body.history, draft, body.message, [str(e) for e in body.errors]):
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# Unsaved drafts rendered in the editor's preview. Kept in memory briefly; the preview frame calls
# the SDK as the real widget instance, so it has exactly that widget's grants (new hosts in a draft
# only work after saving and allowing them).
PREVIEWS: dict[str, dict] = {}
PREVIEW_TTL, PREVIEW_MAX = 1800, 50


class PreviewIn(BaseModel):
    code: str = Field(max_length=store.MAX_CODE)


@app.post("/api/widgets/{wid}/preview")
def create_preview(wid: str, body: PreviewIn):
    with closing(db()) as c:
        store.get_widget_row(c, wid)
    now = time.time()
    for k in [k for k, v in PREVIEWS.items() if now - v["t"] > PREVIEW_TTL]:
        del PREVIEWS[k]
    while len(PREVIEWS) >= PREVIEW_MAX:
        del PREVIEWS[min(PREVIEWS, key=lambda k: PREVIEWS[k]["t"])]
    token = pysecrets.token_urlsafe(16)
    PREVIEWS[token] = {"wid": wid, "code": body.code, "t": now}
    return {"url": f"/frame-preview/{token}"}


@app.get("/frame-preview/{token}")
def frame_preview(token: str, request: Request):
    p = PREVIEWS.get(token)
    if not p or time.time() - p["t"] > PREVIEW_TTL:
        raise HTTPException(404, "preview expired; press Preview again")
    return frame_response(p["code"], "preview", request)


# ---------- AI ----------

class AIConfigIn(BaseModel):
    provider: str
    model: str | None = None
    base_url: str | None = None
    effort: str | None = None
    api_key: str | None = Field(None, max_length=500)  # None = keep, "" = delete


@app.get("/api/ai/config")
def get_ai_config():
    with closing(db()) as c:
        return ai.get_config(c)


@app.put("/api/ai/config")
def put_ai_config(cfg: AIConfigIn):
    with closing(db()) as c:
        return ai.set_config(c, cfg.provider, cfg.model, cfg.base_url, cfg.effort, cfg.api_key)


@app.get("/api/chat")
def get_chat():
    with closing(db()) as c:
        return ai.display_history(c)


@app.delete("/api/chat", status_code=204)
def clear_chat():
    with closing(db()) as c:
        ai.clear_history(c)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    frame_errors: dict[str, str] = {}


@app.post("/api/chat")
async def post_chat(body: ChatIn):
    errors = {k[:20]: v[:300] for k, v in list(body.frame_errors.items())[:20]}

    async def events():
        async for ev in ai.chat(body.message, errors):
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ---------- sandboxed frame ----------

FRAME_CSP = "; ".join([
    "default-src 'none'",
    "script-src 'unsafe-inline'",
    "style-src 'unsafe-inline'",
    "img-src data: blob:",
    "font-src data: 'self' {fonts}",  # bundled fonts only (see frame())
    "media-src data: blob:",
    "connect-src 'none'",
    "frame-src 'none'",
    "form-action 'none'",
    "base-uri 'none'",
    "frame-ancestors 'self'",
])

# Widget base styles use theme tokens only; the active tokens are inlined before this.
FRAME_BASE_CSS = """
html, body { margin: 0; height: 100%; }
body { background: transparent; color: var(--text); font: 14px var(--font);
  padding: 0 12px 12px; box-sizing: border-box; overflow: auto; }
* { box-sizing: border-box; }
a { color: var(--accent); }
h1, h2, h3 { font-family: var(--font-display); }
.muted { color: var(--muted); }
.error { color: var(--danger); }
.label { font: 11px var(--label-font); text-transform: var(--label-case); letter-spacing: var(--label-tracking);
  color: var(--muted); }
"""


@app.get("/frame/{wid}")
def frame(wid: str, request: Request):
    with closing(db()) as c:
        row = store.get_widget_row(c, wid)
        t = json.loads(row["config"]).get("type")
        d = t and store.get_def(c, t)
    return frame_response(d["code"] if d else '<p class="muted">Empty widget</p>', t or "widget", request)


def frame_response(code: str, title: str, request: Request) -> HTMLResponse:
    """A sandboxed widget document: theme tokens, base styles and the SDK, then the widget's code."""
    with closing(db()) as c:
        theme = store.theme_css(store.active_theme(c)["vars"])
    doc = (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>"
        f"<style>{FONTS_CSS}{theme}{FRAME_BASE_CSS}</style><script>{SDK_JS}</script></head>"
        f"<body>{code}</body></html>"
    )
    return HTMLResponse(doc, headers={
        # Only this server's /fonts/ path; widgets still cannot reach anything else.
        "Content-Security-Policy": FRAME_CSP.format(fonts=f"{str(request.base_url).rstrip('/')}/fonts/"),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    })


def _asset_version() -> str:
    """Short hash of every file under static/: changes whenever any of them changes."""
    h = hashlib.sha256()
    for f in sorted(p for p in STATIC.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(STATIC)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


ASSET_VERSION = _asset_version()
INDEX_HTML = (STATIC / "index.html").read_text().replace("{{v}}", ASSET_VERSION)


@app.get("/")
def index():
    # Inline the active theme so the first paint already has the right colors.
    with closing(db()) as c:
        css = store.theme_css(store.active_theme(c)["vars"])
    return HTMLResponse(INDEX_HTML.replace("<!--THEME-->", f'<style id="theme-vars">{css}</style>'),
                        headers={"Cache-Control": "no-store"})


@app.get("/edit/{wid}")
def editor_page(wid: str):
    return index()


app.mount("/", StaticFiles(directory=STATIC), name="static")
