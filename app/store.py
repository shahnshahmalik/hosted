"""SQLite data layer shared by the HTTP routes and the AI tools."""

import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

DB_PATH = os.environ.get("HOSTED_DB") or os.environ.get("CANVAS_DB") or "data/canvas.db"
ROOT = Path(__file__).resolve().parent.parent
WIDGETS_DIR = ROOT / "widgets"

COLS, MAX_ROWS = 12, 200
SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
# Widget type ids. Installed plugins will use a namespaced form, e.g. "gh.owner.repo".
TYPE_ID = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
HOST_PATTERN = re.compile(r"^(\*\.)?[a-z0-9.-]+(:\d{1,5})?$|^\[[0-9a-f:]+\](:\d{1,5})?$")
PROP_TYPES = {"string", "number", "boolean", "select"}
MAX_CODE = 200 * 1024
# Where a widget type came from. Only "ai" types may be modified by the AI.
SOURCES = {"builtin", "ai", "installed"}

THEME_VARS = {"--bg", "--card", "--text", "--muted", "--accent", "--radius", "--font"}
THEME_VALUE = re.compile(r"""^[#(),.%\w\s'"-]{1,120}$""")


class StoreError(ValueError):
    pass


def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _cols(c, table):
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}


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
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chat_messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL,
              created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            """
        )
        if "grants" not in _cols(c, "widgets"):
            # Permissions the user approved for this instance. Never writable by PUT or the AI.
            c.execute("ALTER TABLE widgets ADD COLUMN grants TEXT NOT NULL DEFAULT '{\"hosts\":[],\"secrets\":[]}'")
        defcols = _cols(c, "widget_defs")
        if "version" not in defcols:
            c.execute("ALTER TABLE widget_defs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        if "source" not in defcols:
            c.execute("ALTER TABLE widget_defs ADD COLUMN source TEXT NOT NULL DEFAULT 'ai'")
            c.execute("UPDATE widget_defs SET source='builtin' WHERE builtin=1")
        if "origin" not in defcols:
            # e.g. a GitHub URL + ref for installed plugins; NULL for builtin/ai
            c.execute("ALTER TABLE widget_defs ADD COLUMN origin TEXT")
        c.commit()


# ---------- manifests ----------

def validate_manifest(m: dict, type_id: str) -> dict:
    """Strict validation + normalisation. Used for builtin, AI-made and (later) installed widgets."""
    if not isinstance(m, dict):
        raise StoreError("manifest must be an object")
    perms = m.get("permissions") or {}
    hosts = [str(h).strip().lower() for h in perms.get("hosts", [])]
    for h in hosts:
        if not HOST_PATTERN.match(h) or h in ("*", "*.") or h.count("*") > 1:
            raise StoreError(f"invalid host pattern {h!r}; use e.g. api.example.com, *.example.com, 192.168.1.5:8080")
    secrets = [str(s) for s in perms.get("secrets", [])]
    for s in secrets:
        if not SECRET_NAME.match(s):
            raise StoreError(f"invalid secret name {s!r}; use UPPER_SNAKE_CASE")
    props = {}
    for k, p in (m.get("props") or {}).items():
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]{0,40}$", k) or not isinstance(p, dict):
            raise StoreError(f"invalid prop {k!r}")
        t = p.get("type", "string")
        if t not in PROP_TYPES:
            raise StoreError(f"prop {k}: type must be one of {sorted(PROP_TYPES)}")
        props[k] = {"type": t, "label": str(p.get("label", k))[:80], "default": p.get("default")}
        if t == "select":
            props[k]["options"] = [str(o) for o in p.get("options", [])][:50]
    size = m.get("size") or {}
    w, h = int(size.get("w", 3)), int(size.get("h", 2))
    return {
        "id": type_id,
        "name": str(m.get("name") or type_id)[:60],
        "description": str(m.get("description", ""))[:400],
        "size": {"w": max(1, min(COLS, w)), "h": max(1, min(20, h))},
        "permissions": {"hosts": sorted(set(hosts)), "secrets": sorted(set(secrets))},
        "props": props,
    }


def seed_builtin_widgets():
    """Load widgets/<id>/{manifest.json,index.html}. This folder layout is the widget package format."""
    if not WIDGETS_DIR.is_dir():
        return
    with closing(db()) as c:
        for d in sorted(WIDGETS_DIR.iterdir()):
            mf, code = d / "manifest.json", d / "index.html"
            if not (mf.is_file() and code.is_file()):
                continue
            manifest, code_text = validate_manifest(json.loads(mf.read_text()), d.name), code.read_text()
            old = c.execute("SELECT manifest, code, source FROM widget_defs WHERE id=?", (d.name,)).fetchone()
            if old is None:
                c.execute(
                    "INSERT INTO widget_defs (id, manifest, code, builtin, source, version) VALUES (?,?,?,1,'builtin',1)",
                    (d.name, json.dumps(manifest), code_text),
                )
            elif old["source"] == "builtin" and (json.loads(old["manifest"]) != manifest or old["code"] != code_text):
                c.execute(
                    "UPDATE widget_defs SET manifest=?, code=?, version=version+1 WHERE id=?",
                    (json.dumps(manifest), code_text, d.name),
                )
        c.commit()


def def_row_to_dict(r, with_code=False):
    d = {**json.loads(r["manifest"]), "source": r["source"], "builtin": r["source"] == "builtin",
         "version": r["version"], "origin": r["origin"]}
    if with_code:
        d["code"] = r["code"]
    return d


def get_def(c, type_id, with_code=True):
    r = c.execute("SELECT * FROM widget_defs WHERE id=?", (type_id,)).fetchone()
    return def_row_to_dict(r, with_code) if r else None


def list_defs(c):
    return [def_row_to_dict(r) for r in c.execute("SELECT * FROM widget_defs ORDER BY id")]


def save_def(c, type_id: str, manifest: dict, code: str, source: str, origin: str | None = None) -> dict:
    if not TYPE_ID.match(type_id):
        raise StoreError("type id must be lowercase letters, digits and dashes (2-41 chars)")
    if source not in SOURCES:
        raise StoreError("bad source")
    if not isinstance(code, str) or not code.strip():
        raise StoreError("code is empty")
    if len(code.encode()) > MAX_CODE:
        raise StoreError("code exceeds 200 KB")
    manifest = validate_manifest(manifest, type_id)
    old = c.execute("SELECT source FROM widget_defs WHERE id=?", (type_id,)).fetchone()
    if old and old["source"] != source:
        raise StoreError(f"widget type {type_id!r} is {old['source']} and cannot be overwritten; choose a new id")
    if old:
        c.execute("UPDATE widget_defs SET manifest=?, code=?, origin=?, version=version+1 WHERE id=?",
                  (json.dumps(manifest), code, origin, type_id))
    else:
        c.execute("INSERT INTO widget_defs (id, manifest, code, builtin, source, origin, version) VALUES (?,?,?,0,?,?,1)",
                  (type_id, json.dumps(manifest), code, source, origin))
    c.commit()
    return get_def(c, type_id, with_code=False)


def delete_def(c, type_id: str, source: str):
    d = get_def(c, type_id, with_code=False)
    if not d:
        raise StoreError(f"unknown widget type {type_id!r}")
    if d["source"] != source:
        raise StoreError(f"widget type {type_id!r} is {d['source']} and cannot be deleted here")
    n = c.execute("SELECT COUNT(*) FROM widgets WHERE json_extract(config,'$.type')=?", (type_id,)).fetchone()[0]
    if n:
        raise StoreError(f"{n} widget(s) still use {type_id!r}; remove them first")
    c.execute("DELETE FROM widget_defs WHERE id=?", (type_id,))
    c.commit()


# ---------- widget instances ----------

def _pending(grants: dict, declared: dict) -> dict:
    return {k: [v for v in declared.get(k, []) if v not in grants.get(k, [])] for k in ("hosts", "secrets")}


def row_to_widget(c, r, defs_cache=None):
    config, grants = json.loads(r["config"]), json.loads(r["grants"])
    t = config.get("type")
    d = None
    if t:
        d = defs_cache.get(t) if defs_cache is not None else get_def(c, t, with_code=False)
    return {
        **{k: r[k] for k in ("id", "title", "x", "y", "w", "h")},
        "config": config,
        "grants": grants,
        # Declared by the widget type but not yet approved by the user.
        "pending": _pending(grants, d["permissions"]) if d else {"hosts": [], "secrets": []},
    }


def get_widget_row(c, wid):
    r = c.execute("SELECT * FROM widgets WHERE id=?", (wid,)).fetchone()
    if not r:
        raise StoreError(f"widget {wid!r} not found")
    return r


def list_widgets(c):
    defs = {d["id"]: d for d in list_defs(c)}
    return [row_to_widget(c, r, defs) for r in c.execute("SELECT * FROM widgets ORDER BY y, x")]


def overlaps(a, b):
    return a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]


def check_rect(r):
    for k in ("x", "y", "w", "h"):
        if not isinstance(r.get(k), int) or isinstance(r.get(k), bool):
            raise StoreError(f"{k} must be an integer")
    if r["x"] < 0 or r["y"] < 0 or r["w"] < 1 or r["h"] < 1 or r["h"] > 20 or r["y"] > MAX_ROWS:
        raise StoreError("position out of range (x,y >= 0; w >= 1; 1 <= h <= 20)")
    if r["x"] + r["w"] > COLS:
        raise StoreError(f"x + w must be <= {COLS}")


def collisions(c, rect, ignore=()):
    return [dict(o) for o in c.execute("SELECT id, title, x, y, w, h FROM widgets")
            if o["id"] not in ignore and overlaps(rect, o)]


def free_slot(c, w, h):
    others = [dict(o) for o in c.execute("SELECT x, y, w, h FROM widgets")]
    for y in range(MAX_ROWS):
        for x in range(COLS - w + 1):
            cand = {"x": x, "y": y, "w": w, "h": h}
            if not any(overlaps(cand, o) for o in others):
                return cand
    raise StoreError("dashboard is full")


def create_widget(c, title, config, rect=None, grant=False):
    """grant=True only for direct user actions (the picker). The AI always creates ungranted widgets."""
    grants = {"hosts": [], "secrets": []}
    t = config.get("type")
    d = None
    if t:
        d = get_def(c, t, with_code=False)
        if not d:
            raise StoreError(f"unknown widget type {t!r}")
        if grant:
            grants = d["permissions"]
    if rect is None:
        size = d["size"] if d else {"w": 3, "h": 2}
        rect = free_slot(c, size["w"], size["h"])
    check_rect(rect)
    if hit := collisions(c, rect):
        raise StoreError(f"position overlaps {', '.join(o['id'] + ' (' + o['title'] + ')' for o in hit)}")
    wid = uuid.uuid4().hex[:12]
    c.execute("INSERT INTO widgets (id, title, x, y, w, h, config, grants) VALUES (?,?,?,?,?,?,?,?)",
              (wid, title, rect["x"], rect["y"], rect["w"], rect["h"], json.dumps(config), json.dumps(grants)))
    c.commit()
    return row_to_widget(c, get_widget_row(c, wid))


def update_widget(c, wid, title=None, rect=None, config=None, check_overlap=True):
    r = get_widget_row(c, wid)
    old_cfg = json.loads(r["config"])
    if config is not None and config.get("type") != old_cfg.get("type"):
        raise StoreError("widget type cannot be changed; add a new widget instead")
    if rect is not None:
        check_rect(rect)
        if check_overlap and (hit := collisions(c, rect, ignore=(wid,))):
            raise StoreError(f"position overlaps {', '.join(o['id'] + ' (' + o['title'] + ')' for o in hit)}")
    new = {"title": title if title is not None else r["title"], **(rect or {k: r[k] for k in ("x", "y", "w", "h")})}
    c.execute("UPDATE widgets SET title=?, x=?, y=?, w=?, h=?, config=? WHERE id=?",
              (new["title"], new["x"], new["y"], new["w"], new["h"], json.dumps(config if config is not None else old_cfg), wid))
    c.commit()
    return row_to_widget(c, get_widget_row(c, wid))


def delete_widget(c, wid):
    c.execute("DELETE FROM widgets WHERE id=?", (wid,))
    c.execute("DELETE FROM widget_storage WHERE wid=?", (wid,))
    c.commit()


def grant_widget(c, wid):
    """User approval: grant everything the widget type currently declares."""
    r = get_widget_row(c, wid)
    t = json.loads(r["config"]).get("type")
    d = t and get_def(c, t, with_code=False)
    if not d:
        raise StoreError("widget has no type")
    c.execute("UPDATE widgets SET grants=? WHERE id=?", (json.dumps(d["permissions"]), wid))
    c.commit()
    return row_to_widget(c, get_widget_row(c, wid))


def effective_grants(c, row) -> dict:
    """Granted ∩ currently declared: a redefined widget can never silently gain access."""
    granted = json.loads(row["grants"])
    t = json.loads(row["config"]).get("type")
    d = t and get_def(c, t, with_code=False)
    if not d:
        return {"hosts": [], "secrets": []}
    declared = d["permissions"]
    return {k: [v for v in granted.get(k, []) if v in declared.get(k, [])] for k in ("hosts", "secrets")}


# ---------- settings, theme, secrets ----------

def get_setting(c, key, default=None):
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_setting(c, key, value):
    c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))
    c.commit()


def validate_theme(vars_: dict) -> dict:
    out = {}
    for k, v in (vars_ or {}).items():
        if k not in THEME_VARS:
            raise StoreError(f"unknown theme variable {k}; allowed: {', '.join(sorted(THEME_VARS))}")
        v = str(v).strip()
        if not THEME_VALUE.match(v) or "url(" in v.lower() or "expression" in v.lower():
            raise StoreError(f"invalid value for {k}: {v!r}")
        out[k] = v
    return out


def secret_names(c):
    return [r["name"] for r in c.execute("SELECT name FROM secrets ORDER BY name")]
