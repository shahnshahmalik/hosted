"""AI chat for the widget editor.

Stateless on the server: the page sends the prior conversation as plain text plus the current
draft (manifest + code) with every message. Tools edit that draft in memory and each change is
streamed back as a "draft" event; nothing is saved until the user presses Save.
"""

import asyncio
import json
from contextlib import closing
from pathlib import Path

from . import ai, store
from .store import StoreError

EDIT_PROMPT = (Path(__file__).parent / "prompt_edit.md").read_text() + "\n\n" + ai.WIDGET_GUIDE
MAX_ROUNDS = 12
MAX_HISTORY = 30  # prior user/assistant messages kept (text only)
_lock = asyncio.Lock()

EDIT_TOOLS = [
    {
        "name": "edit_code",
        "description": "Apply exact find/replace edits to the widget's index.html, in order. Each `find` must "
                       "occur exactly once in the current code (include enough surrounding text to be unique).",
        "input_schema": {
            "type": "object",
            "properties": {"edits": {"type": "array", "items": {
                "type": "object",
                "properties": {"find": {"type": "string"}, "replace": {"type": "string"}},
                "required": ["find", "replace"]}}},
            "required": ["edits"],
        },
    },
    {
        "name": "write_code",
        "description": "Replace the widget's whole index.html. Use for rewrites; prefer edit_code for small changes.",
        "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
    },
    {
        "name": "update_manifest",
        "description": "Change manifest fields. Given fields replace the current value; omitted ones are kept. "
                       "`props` replaces the whole settings schema, so include existing props you want to keep.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"}, "description": {"type": "string"},
                "size": {"type": "object", "properties": {"w": {"type": "integer"}, "h": {"type": "integer"}}},
                "hosts": {"type": "array", "items": {"type": "string"}},
                "secrets": {"type": "array", "items": {"type": "string"}},
                "capabilities": {"type": "array", "items": {"type": "string", "enum": sorted(store.CAPABILITIES)}},
                "props": {"type": "object"},
            },
        },
    },
]


def apply_tool(name: str, args, draft: dict) -> tuple[str, str]:
    """Mutates draft {"manifest", "code"}. Returns (result for the model, short summary for the UI)."""
    if not isinstance(args, dict):
        raise StoreError("tool input must be an object")
    if name == "edit_code":
        edits = args.get("edits")
        if not isinstance(edits, list) or not edits:
            raise StoreError("edits must be a non-empty array")
        code = draft["code"]
        for i, e in enumerate(edits):
            if not isinstance(e, dict) or not isinstance(e.get("find"), str) or not isinstance(e.get("replace"), str):
                raise StoreError(f"edit {i + 1}: find and replace must be strings")
            n = code.count(e["find"]) if e["find"] else 0
            if n != 1:
                raise StoreError(f"edit {i + 1}: `find` matched {n} times; it must match exactly once. "
                                 "Copy the text exactly from <current_widget> and include more context.")
            code = code.replace(e["find"], e["replace"], 1)
        if len(code.encode()) > store.MAX_CODE:
            raise StoreError("code would exceed 200 KB")
        draft["code"] = code
        return json.dumps({"applied": len(edits), "code_bytes": len(code.encode())}), \
            f"Edited code ({len(edits)} change{'s' if len(edits) != 1 else ''})"
    if name == "write_code":
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            raise StoreError("code must be a non-empty string")
        if len(code.encode()) > store.MAX_CODE:
            raise StoreError("code exceeds 200 KB")
        draft["code"] = code
        return json.dumps({"written": len(code.encode())}), "Rewrote the code"
    if name == "update_manifest":
        m = json.loads(json.dumps(draft["manifest"]))
        perms = m.setdefault("permissions", {})
        for k in ("name", "description", "size", "props"):
            if k in args:
                m[k] = args[k]
        for k in ("hosts", "secrets", "capabilities"):
            if k in args:
                perms[k] = args[k]
        validated = store.validate_manifest(m, draft.get("type_id") or "draft")  # raises on bad input
        validated.pop("id", None)
        draft["manifest"] = validated
        return json.dumps({"manifest": validated}), "Updated the manifest"
    raise StoreError(f"unknown tool {name}")


def _user_turn(message: str, draft: dict, errors: list[str]) -> str:
    widget = json.dumps({"manifest": draft["manifest"]}, indent=1)
    parts = [message, "", "<current_widget>", widget, "index.html:", draft["code"], "</current_widget>"]
    if errors:
        parts += ["<preview_errors>", *[e[:300] for e in errors[:10]], "</preview_errors>"]
    return "\n".join(parts)


async def edit_chat(wid: str, history: list[dict], draft: dict, message: str, errors: list[str]):
    """Async generator of events: text, tool_start, tool, draft, error, done."""
    if _lock.locked():
        yield {"type": "error", "message": "The editor assistant is busy with another request."}
        return
    async with _lock:
        with closing(store.db()) as c:
            store.get_widget_row(c, wid)
            cfg = ai.get_config(c)
            key = store.get_setting(c, f"ai_key:{cfg['provider']}")
        if cfg["provider"] != "ollama" and not key:
            yield {"type": "error", "message": "Add an API key in the assistant's settings on the dashboard first."}
            return

        rows = []
        for h in (history or [])[-MAX_HISTORY:]:
            role, text = h.get("role"), str(h.get("text") or "")[:8000]
            if role in ("user", "assistant") and text:
                rows.append({"role": role, "text": text, "tool_calls": []} if role == "assistant" else {"role": "user", "text": text})
        rows.append({"role": "user", "text": _user_turn(message, draft, errors)})

        turn = ai._anthropic_turn if cfg["provider"] == "anthropic" else ai._openai_turn
        for _ in range(MAX_ROUNDS):
            final = None
            try:
                async for ev in turn(cfg, key, rows, system=EDIT_PROMPT, tool_defs=EDIT_TOOLS):
                    if ev["type"] == "_final":
                        final = ev
                    else:
                        yield ev
            except ai.AIError as e:
                yield {"type": "error", "message": str(e)}
                break
            row = final["row"]
            rows.append(row)
            if final["stop"] == "refusal":
                yield {"type": "error", "message": "The model declined this request."}
                break
            if final["stop"] == "truncated":
                yield {"type": "error", "message": "The response was cut off before the edit was complete. Try a smaller change."}
                break
            if not row["tool_calls"]:
                break
            results, changed = [], False
            for call in row["tool_calls"]:
                try:
                    if isinstance(call["args"], dict) and "__invalid_json__" in call["args"]:
                        raise StoreError("tool arguments were not valid JSON")
                    content, summary = apply_tool(call["name"], call["args"], draft)
                    changed = True
                    res = {"id": call["id"], "name": call["name"], "content": content}
                    yield {"type": "tool", "name": call["name"], "ok": True, "summary": summary}
                except StoreError as e:
                    res = {"id": call["id"], "name": call["name"], "content": f"Error: {e}", "is_error": True}
                    yield {"type": "tool", "name": call["name"], "ok": False, "summary": str(e)}
                results.append(res)
            rows.append({"role": "tool", "results": results})
            if changed:
                yield {"type": "draft", "manifest": draft["manifest"], "code": draft["code"]}
        else:
            yield {"type": "error", "message": f"Stopped after {MAX_ROUNDS} steps."}
        yield {"type": "done"}
