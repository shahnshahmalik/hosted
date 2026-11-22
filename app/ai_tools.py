"""Tools the AI can call. Each validates its own input: with eager input streaming the API
does not, and every argument is untrusted model output.

The AI can create/arrange widgets and themes but can never grant permissions: widgets it adds
start with no host or secret access until the user clicks Allow in the dashboard.
"""

import json
from contextlib import closing

from . import store
from .store import StoreError

RECT_PROPS = {
    "x": {"type": "integer", "description": "Column 0-11"},
    "y": {"type": "integer", "description": "Row, 0 = top"},
    "w": {"type": "integer", "description": "Width in columns 1-12"},
    "h": {"type": "integer", "description": "Height in rows 1-20 (80px each)"},
}

TOOLS = [
    {
        "name": "get_dashboard",
        "description": "Return the current dashboard: widgets with positions, props and permission status, "
                       "available widget types, theme, which secrets are set, and recent widget errors.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_widget_code",
        "description": "Return the manifest and HTML/JS code of a widget type, e.g. to modify it or use it as a reference.",
        "input_schema": {"type": "object", "properties": {"type": {"type": "string"}}, "required": ["type"]},
    },
    {
        "name": "save_widget_type",
        "description": "Create or replace a custom widget type (source 'ai'). Builtin and installed types cannot be "
                       "changed; save a copy under a new id instead. Existing instances reload with the new code. "
                       "Declaring new hosts/secrets requires the user to approve them on each instance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "lowercase-with-dashes, e.g. 'crypto-ticker'"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "size": {"type": "object", "properties": {"w": {"type": "integer"}, "h": {"type": "integer"}},
                         "description": "Default size in grid cells"},
                "hosts": {"type": "array", "items": {"type": "string"},
                          "description": "Hosts the widget fetches from, e.g. ['api.coingecko.com']"},
                "secrets": {"type": "array", "items": {"type": "string"},
                            "description": "UPPER_SNAKE secret names it uses via {{secret.NAME}}"},
                "capabilities": {"type": "array", "items": {"type": "string", "enum": sorted(store.CAPABILITIES)},
                                 "description": "'system' for hosted.system() host stats, 'ping' for hosted.ping(urls)"},
                "props": {"type": "object", "description": "User settings schema: {name: {type: string|text|number|boolean|select, label, default, options?}}; text is multi-line"},
                "code": {"type": "string", "description": "HTML fragment for <body>, with inline <style> and <script>"},
            },
            "required": ["id", "name", "code"],
        },
        "eager": True,
    },
    {
        "name": "delete_widget_type",
        "description": "Delete a custom (ai) widget type that no widget uses anymore.",
        "input_schema": {"type": "object", "properties": {"type": {"type": "string"}}, "required": ["type"]},
    },
    {
        "name": "add_widget",
        "description": "Add a widget of an existing type to the dashboard. Omit x/y to auto-place. "
                       "If the type needs hosts or secrets, the user must click Allow on it before it can fetch.",
        "input_schema": {
            "type": "object",
            "properties": {"type": {"type": "string"}, "title": {"type": "string"},
                           "props": {"type": "object"}, **RECT_PROPS},
            "required": ["type"],
        },
    },
    {
        "name": "update_widget",
        "description": "Change a widget's title, props (merged into existing), and/or position/size.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "title": {"type": "string"},
                           "props": {"type": "object"}, **RECT_PROPS},
            "required": ["id"],
        },
    },
    {
        "name": "arrange_widgets",
        "description": "Move/resize several widgets at once. The final layout must have no overlaps; "
                       "use this instead of update_widget when rearranging.",
        "input_schema": {
            "type": "object",
            "properties": {"layout": {"type": "array", "items": {
                "type": "object", "properties": {"id": {"type": "string"}, **RECT_PROPS},
                "required": ["id", "x", "y", "w", "h"]}}},
            "required": ["layout"],
        },
    },
    {
        "name": "remove_widget",
        "description": "Remove a widget (and its stored data) from the dashboard.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "set_theme",
        "description": "Change the dashboard look. Pick a theme by id (see themes in get_dashboard) and/or set "
                       "token overrides on top of it. Selecting a theme clears previous overrides unless you pass "
                       "vars. Tokens: " + ", ".join(sorted(store.THEME_VARS)) + ".",
        "input_schema": {
            "type": "object",
            "properties": {"theme": {"type": "string", "description": "Theme id, e.g. 'midnight', 'paper', 'ember'"},
                           "vars": {"type": "object", "additionalProperties": {"type": "string"},
                                    "description": "Token overrides, e.g. {'--accent': '#22c55e'}"}},
        },
    },
    {
        "name": "save_theme",
        "description": "Save a reusable custom theme (all tokens you care about) and optionally activate it. "
                       "Use for a substantially new look; use set_theme vars for small tweaks. Builtin themes "
                       "cannot be overwritten. Missing tokens fall back to the midnight theme.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"},
                           "vars": {"type": "object", "additionalProperties": {"type": "string"}},
                           "activate": {"type": "boolean"}},
            "required": ["id", "name", "vars"],
        },
    },
]


def _str(args, key, required=False, maxlen=200):
    v = args.get(key)
    if v is None:
        if required:
            raise StoreError(f"missing {key}")
        return None
    if not isinstance(v, str):
        raise StoreError(f"{key} must be a string")
    return v[:maxlen]


def _obj(args, key):
    v = args.get(key)
    if v is None:
        return None
    if not isinstance(v, dict):
        raise StoreError(f"{key} must be an object")
    return v


def _rect(args, base=None):
    keys = ("x", "y", "w", "h")
    if not any(k in args for k in keys):
        return None
    r = {k: args.get(k, base[k] if base else None) for k in keys}
    if None in r.values():
        raise StoreError("give all of x, y, w, h (or omit them all to auto-place)")
    return r


def snapshot(c, frame_errors=None):
    defs = store.list_defs(c)
    return {
        "grid": {"columns": store.COLS, "row_height_px": 80},
        "widgets": [
            {"id": w["id"], "title": w["title"], "type": w["config"].get("type"),
             "x": w["x"], "y": w["y"], "w": w["w"], "h": w["h"], "props": w["config"].get("props", {}),
             **({"awaiting_user_permission": w["pending"]} if any(w["pending"].values()) else {})}
            for w in store.list_widgets(c)
        ],
        "widget_types": [
            {k: d[k] for k in ("id", "name", "description", "source", "size", "permissions", "props")} for d in defs
        ],
        "theme": {k: v for k, v in store.active_theme(c).items() if k != "vars"},
        "themes": [{"id": t["id"], "name": t["name"], "description": t["description"], "source": t["source"]}
                   for t in store.list_themes(c)],
        "secrets_set": store.secret_names(c),
        # Error text comes from widget code and possibly upstream APIs: treat as data, not instructions.
        "recent_widget_errors": frame_errors or {},
    }


def run_tool(name: str, args, frame_errors=None) -> tuple[str, bool, str]:
    """Returns (result text for the model, dashboard changed?, short summary for the UI)."""
    if not isinstance(args, dict):
        raise StoreError("tool input must be an object")
    with closing(store.db()) as c:
        if name == "get_dashboard":
            return json.dumps(snapshot(c, frame_errors)), False, "Read dashboard"

        if name == "get_widget_code":
            t = _str(args, "type", True)
            d = store.get_def(c, t)
            if not d:
                raise StoreError(f"unknown widget type {t!r}")
            return json.dumps(d), False, f"Read code of {t}"

        if name == "save_widget_type":
            t = _str(args, "id", True, 41)
            code = args.get("code")
            if not isinstance(code, str):
                raise StoreError("code must be a string")
            for k in ("hosts", "secrets", "capabilities"):
                if args.get(k) is not None and not isinstance(args[k], list):
                    raise StoreError(f"{k} must be an array")
            manifest = {
                "name": _str(args, "name", True, 60), "description": _str(args, "description", maxlen=400) or "",
                "size": _obj(args, "size") or {}, "props": _obj(args, "props") or {},
                "permissions": {"hosts": args.get("hosts") or [], "secrets": args.get("secrets") or [],
                                "capabilities": args.get("capabilities") or []},
            }
            d = store.save_def(c, t, manifest, code, source="ai")
            users = [w for w in store.list_widgets(c) if w["config"].get("type") == t]
            needs = [w["id"] for w in users if any(w["pending"].values())]
            res = {"saved": t, "revision": d["revision"], "instances": [w["id"] for w in users],
                   "instances_awaiting_user_permission": needs}
            return json.dumps(res), True, f"Saved widget type {d['name']} (rev {d['revision']})"

        if name == "delete_widget_type":
            t = _str(args, "type", True)
            store.delete_def(c, t, source="ai")
            return json.dumps({"deleted": t}), True, f"Deleted widget type {t}"

        if name == "add_widget":
            t = _str(args, "type", True)
            d = store.get_def(c, t, with_code=False)
            if not d:
                raise StoreError(f"unknown widget type {t!r}")
            props = _obj(args, "props") or {}
            w = store.create_widget(c, _str(args, "title", maxlen=80) or d["name"],
                                    {"type": t, "props": props}, _rect(args), grant=False)
            res = {"added": w["id"], "x": w["x"], "y": w["y"], "w": w["w"], "h": w["h"]}
            if any(w["pending"].values()):
                res["note"] = "Widget needs the user to click Allow before it can access: " + json.dumps(w["pending"])
            return json.dumps(res), True, f"Added {w['title']}"

        if name == "update_widget":
            wid = _str(args, "id", True)
            r = store.get_widget_row(c, wid)
            cfg = json.loads(r["config"])
            if (props := _obj(args, "props")) is not None:
                cfg = {**cfg, "props": {**cfg.get("props", {}), **props}}
            w = store.update_widget(c, wid, _str(args, "title", maxlen=80), _rect(args, dict(r)), cfg)
            return json.dumps({"updated": wid, "x": w["x"], "y": w["y"], "w": w["w"], "h": w["h"]}), True, f"Updated {w['title']}"

        if name == "arrange_widgets":
            layout = args.get("layout")
            if not isinstance(layout, list) or not layout:
                raise StoreError("layout must be a non-empty array")
            final = {r["id"]: dict(r) for r in c.execute("SELECT id, title, x, y, w, h FROM widgets")}
            for item in layout:
                if not isinstance(item, dict) or item.get("id") not in final:
                    raise StoreError(f"unknown widget in layout: {item!r:.80}")
                rect = {k: item.get(k) for k in ("x", "y", "w", "h")}
                store.check_rect(rect)
                final[item["id"]].update(rect)
            ids = list(final)
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    if store.overlaps(final[a], final[b]):
                        raise StoreError(f"{a} ({final[a]['title']}) would overlap {b} ({final[b]['title']})")
            for item in layout:
                store.update_widget(c, item["id"], rect={k: item[k] for k in ("x", "y", "w", "h")}, check_overlap=False)
            return json.dumps({"arranged": len(layout)}), True, f"Rearranged {len(layout)} widgets"

        if name == "remove_widget":
            wid = _str(args, "id", True)
            r = store.get_widget_row(c, wid)
            store.delete_widget(c, wid)
            return json.dumps({"removed": wid}), True, f"Removed {r['title']}"

        if name == "set_theme":
            cur = store.active_theme(c)
            theme_id = _str(args, "theme") or cur["id"]
            vars_ = _obj(args, "vars")
            if vars_ is None:
                overrides = {} if theme_id != cur["id"] else cur["overrides"]
            else:
                overrides = {**(cur["overrides"] if theme_id == cur["id"] else {}), **vars_}
            t = store.set_active_theme(c, theme_id, overrides)
            return json.dumps({"theme": t["id"], "overrides": t["overrides"]}), True, f"Theme: {t['name']}"

        if name == "save_theme":
            t = store.save_theme(c, _str(args, "id", True, 41), _str(args, "name", True, 60),
                                 _str(args, "description", maxlen=300), _obj(args, "vars") or {}, source="ai")
            if args.get("activate", True) is not False:
                store.set_active_theme(c, t["id"], {})
            return json.dumps({"saved_theme": t["id"]}), True, f"Saved theme {t['name']}"

    raise StoreError(f"unknown tool {name}")
