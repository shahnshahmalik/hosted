"""AI chat: provider adapters (Anthropic SDK; OpenAI-compatible HTTP for OpenAI and Ollama)
and the tool-calling loop.

History is stored provider-neutrally and append-only in chat_messages. Anthropic assistant
turns also keep their raw content blocks (thinking, fallback markers) so they can be passed
back unchanged, as the API requires.
"""

import asyncio
import json
import re
import uuid
from contextlib import closing
from pathlib import Path

import anthropic
import httpx

from . import store
from .ai_tools import TOOLS, run_tool, snapshot
from .store import StoreError

_HERE = Path(__file__).parent
# The widget-authoring guide is shared by the dashboard assistant and the widget editor.
WIDGET_GUIDE = (_HERE / "prompt_widgets.md").read_text()
SYSTEM_PROMPT = (_HERE / "prompt.md").read_text() + "\n\n" + WIDGET_GUIDE
PROVIDERS = {
    "anthropic": {"model": "claude-opus-5-5", "base_url": None},
    "openai": {"model": "", "base_url": "https://api.openai.com/v1"},
    "ollama": {"model": "", "base_url": "http://localhost:11434/v1"},
}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
# Models that accept server-side refusal fallbacks with fallbacks="default".
FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
MAX_ROUNDS = 20
MAX_HISTORY_ROWS = 600
_lock = asyncio.Lock()


class AIError(Exception):
    pass


# ---------- config ----------

def get_config(c) -> dict:
    cfg = store.get_setting(c, "ai", {}) or {}
    provider = cfg.get("provider") if cfg.get("provider") in PROVIDERS else "anthropic"
    d = PROVIDERS[provider]
    return {
        "provider": provider,
        "model": cfg.get("model") or d["model"],
        "base_url": cfg.get("base_url") or d["base_url"],
        "effort": cfg.get("effort") if cfg.get("effort") in EFFORTS else "medium",
        "has_key": bool(store.get_setting(c, f"ai_key:{provider}")),
    }


def set_config(c, provider, model=None, base_url=None, effort=None, api_key=None):
    if provider not in PROVIDERS:
        raise StoreError(f"provider must be one of {', '.join(PROVIDERS)}")
    if effort is not None and effort not in EFFORTS:
        raise StoreError("invalid effort")
    if base_url and not re.match(r"^https?://", base_url):
        raise StoreError("base URL must start with http:// or https://")
    store.set_setting(c, "ai", {"provider": provider, "model": (model or "").strip(),
                                "base_url": (base_url or "").strip().rstrip("/"), "effort": effort})
    if api_key is not None:
        if api_key == "":
            c.execute("DELETE FROM settings WHERE key=?", (f"ai_key:{provider}",))
            c.commit()
        else:
            store.set_setting(c, f"ai_key:{provider}", api_key.strip())
    return get_config(c)


# ---------- history ----------

def load_history(c) -> list[dict]:
    rows = [json.loads(r["data"]) for r in c.execute("SELECT data FROM chat_messages ORDER BY id")]
    # Repair (append-only) an assistant turn whose tool calls never got results, e.g. after a disconnect.
    if rows and rows[-1]["role"] == "assistant" and rows[-1].get("tool_calls"):
        fix = {"role": "tool", "results": [
            {"id": tc["id"], "name": tc["name"], "content": "Interrupted before this tool ran.", "is_error": True}
            for tc in rows[-1]["tool_calls"]]}
        append(c, fix)
        rows.append(fix)
    return rows


def append(c, row: dict):
    c.execute("INSERT INTO chat_messages (data) VALUES (?)", (json.dumps(row),))
    c.commit()


def display_history(c) -> list[dict]:
    out = []
    for r in load_history(c):
        if r["role"] == "user":
            out.append({"role": "user", "text": r.get("display", r["text"])})
        elif r["role"] == "assistant" and r.get("text"):
            out.append({"role": "assistant", "text": r["text"]})
        elif r["role"] == "tool":
            for res in r["results"]:
                out.append({"role": "tool", "name": res["name"], "ok": not res.get("is_error"),
                            "summary": res.get("summary") or res["content"][:200]})
        if r.get("error"):
            out.append({"role": "error", "text": r["error"]})
    return out


def clear_history(c):
    c.execute("DELETE FROM chat_messages")
    c.commit()


# ---------- Anthropic ----------

def _safe_id(i: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", i or "") or "call_" + uuid.uuid4().hex[:12]


def _to_anthropic(rows):
    msgs = []
    for r in rows:
        if r["role"] == "user":
            msgs.append({"role": "user", "content": r["text"]})
        elif r["role"] == "assistant":
            if r.get("provider") == "anthropic" and r.get("raw"):
                content = r["raw"]  # unchanged, including thinking and fallback blocks
            else:
                content = ([{"type": "text", "text": r["text"]}] if r.get("text") else []) + [
                    {"type": "tool_use", "id": _safe_id(tc["id"]), "name": tc["name"], "input": tc["args"]}
                    for tc in r.get("tool_calls", [])]
            if content:
                msgs.append({"role": "assistant", "content": content})
        elif r["role"] == "tool":
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": _safe_id(x["id"]), "content": x["content"],
                 **({"is_error": True} if x.get("is_error") else {})} for x in r["results"]]})
    return msgs


def _after_last_fallback(blocks: list[dict]) -> list[dict]:
    """After a mid-output fallback, blocks before the last `fallback` marker belong to the declined attempt."""
    idx = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=-1)
    return blocks[idx + 1:]


def _echo_safe(blocks: list[dict]) -> list[dict]:
    """Omit thinking/tool_use blocks before the last fallback marker when echoing (marker itself stays)."""
    idx = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=-1)
    drop = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}
    return [b for i, b in enumerate(blocks) if i >= idx or b.get("type") not in drop]


async def _anthropic_turn(cfg, key, rows, system=None, tool_defs=None):
    client = anthropic.AsyncAnthropic(api_key=key, max_retries=2)
    tools = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"],
              "eager_input_streaming": True} for t in (tool_defs or TOOLS)]
    kwargs = dict(model=cfg["model"], max_tokens=64000, system=system or SYSTEM_PROMPT, tools=tools,
                  messages=_to_anthropic(rows), cache_control={"type": "ephemeral"})
    if not cfg["model"].startswith(("claude-haiku-4", "claude-3", "claude-sonnet-4", "claude-opus-4-1")):
        kwargs["output_config"] = {"effort": cfg["effort"]}
    if cfg["model"] in FALLBACK_MODELS:
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")

    for attempt in range(3):
        try:
            async with client.beta.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if event.type == "text":
                        yield {"type": "text", "text": event.text}
                    elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                        yield {"type": "tool_start", "name": event.content_block.name}
                msg = await stream.get_final_message()
            break
        except ValueError:
            # Tool input JSON the SDK could not parse at all (eager streaming): re-issue the turn.
            if attempt == 2:
                raise AIError("the model produced unparseable tool input three times")
            yield {"type": "retry"}
        except anthropic.AuthenticationError:
            raise AIError("Anthropic rejected the API key. Check it in AI settings.")
        except anthropic.PermissionDeniedError as e:
            raise AIError(f"Anthropic denied the request: {e.message}")
        except anthropic.NotFoundError:
            raise AIError(f"Unknown model {cfg['model']!r}.")
        except anthropic.RateLimitError:
            raise AIError("Anthropic rate limit reached; try again in a minute.")
        except anthropic.BadRequestError as e:
            raise AIError(f"Anthropic rejected the request: {e.message}")
        except anthropic.APIStatusError as e:
            raise AIError(f"Anthropic API error {e.status_code}: {e.message}")
        except anthropic.APIConnectionError:
            raise AIError("Could not reach the Anthropic API.")

    raw = [b.model_dump(mode="json", exclude_none=True) for b in msg.content]
    live = _after_last_fallback(raw)
    text = "".join(b.get("text", "") for b in live if b.get("type") == "text").strip()
    calls = [{"id": b["id"], "name": b["name"], "args": b.get("input")} for b in live if b.get("type") == "tool_use"]
    stop = "ok"
    if msg.stop_reason == "refusal":
        stop, calls = "refusal", []
    elif msg.stop_reason == "max_tokens" and calls:
        stop, calls = "truncated", []
    row = {"role": "assistant", "provider": "anthropic", "model": msg.model, "text": text,
           "tool_calls": calls, "raw": _echo_safe(raw)}
    yield {"type": "_final", "row": row, "stop": stop}


# ---------- OpenAI-compatible (OpenAI, Ollama) ----------

def _to_openai(rows, system=None):
    msgs = [{"role": "system", "content": system or SYSTEM_PROMPT}]
    for r in rows:
        if r["role"] == "user":
            msgs.append({"role": "user", "content": r["text"]})
        elif r["role"] == "assistant":
            m = {"role": "assistant", "content": r.get("text") or ""}
            if r.get("tool_calls"):
                m["tool_calls"] = [{"id": tc["id"], "type": "function",
                                    "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])}}
                                   for tc in r["tool_calls"]]
            msgs.append(m)
        elif r["role"] == "tool":
            msgs += [{"role": "tool", "tool_call_id": x["id"], "content": x["content"]} for x in r["results"]]
    return msgs


async def _openai_turn(cfg, key, rows, system=None, tool_defs=None):
    if not cfg["model"]:
        raise AIError("Set a model name in AI settings.")
    tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                               "parameters": t["input_schema"]}} for t in (tool_defs or TOOLS)]
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
            r = await client.post(url, headers=headers,
                                  json={"model": cfg["model"], "messages": _to_openai(rows, system), "tools": tools})
    except httpx.HTTPError as e:
        raise AIError(f"Could not reach {cfg['base_url']}: {type(e).__name__}")
    if r.status_code == 401:
        raise AIError("The provider rejected the API key. Check it in AI settings.")
    if r.status_code >= 400:
        raise AIError(f"Provider error {r.status_code}: {r.text[:300]}")
    try:
        choice = r.json()["choices"][0]
        m = choice["message"]
    except (ValueError, KeyError, IndexError):
        raise AIError(f"Unexpected response from provider: {r.text[:300]}")
    text = (m.get("content") or "").strip()
    if text:
        yield {"type": "text", "text": text}
    calls = []
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function", {})
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except ValueError:
                args = {"__invalid_json__": args[:2000]}
        calls.append({"id": tc.get("id") or "call_" + uuid.uuid4().hex[:12], "name": fn.get("name", ""), "args": args})
    stop = "truncated" if choice.get("finish_reason") == "length" and calls else "ok"
    yield {"type": "_final", "stop": stop, "row": {
        "role": "assistant", "provider": cfg["provider"], "model": cfg["model"], "text": text,
        "tool_calls": [] if stop != "ok" else calls}}


# ---------- loop ----------

async def chat(user_text: str, frame_errors: dict | None):
    """Async generator of UI events: text, tool_start, tool, changed, error, done."""
    if _lock.locked():
        yield {"type": "error", "message": "The assistant is already working on a request."}
        return
    async with _lock:
        with closing(store.db()) as c:
            cfg = get_config(c)
            key = store.get_setting(c, f"ai_key:{cfg['provider']}")
            if cfg["provider"] != "ollama" and not key:
                yield {"type": "error", "message": "Add an API key in AI settings first."}
                return
            history = load_history(c)
            if len(history) > MAX_HISTORY_ROWS:
                yield {"type": "error", "message": "This conversation is very long. Start a new chat."}
                return
            state = json.dumps(snapshot(c, frame_errors), separators=(",", ":"))
            append(c, {"role": "user", "display": user_text,
                       "text": f"<dashboard_state>\n{state}\n</dashboard_state>\n\n{user_text}"})

        turn = _anthropic_turn if cfg["provider"] == "anthropic" else _openai_turn
        for _ in range(MAX_ROUNDS):
            with closing(store.db()) as c:
                rows = load_history(c)
            final = None
            try:
                async for ev in turn(cfg, key, rows):
                    if ev["type"] == "_final":
                        final = ev
                    else:
                        yield ev
            except AIError as e:
                yield {"type": "error", "message": str(e)}
                break
            row = final["row"]
            if final["stop"] == "refusal":
                row["error"] = "The model declined this request."
            elif final["stop"] == "truncated":
                row["error"] = "The response was cut off before the tool input was complete. Try asking for a smaller widget."
            with closing(store.db()) as c:
                append(c, row)
            if row.get("error"):
                yield {"type": "error", "message": row["error"]}
                break
            if not row["tool_calls"]:
                break

            results, changed = [], False
            for call in row["tool_calls"]:
                try:
                    if isinstance(call["args"], dict) and "__invalid_json__" in call["args"]:
                        raise StoreError("tool arguments were not valid JSON: " + call["args"]["__invalid_json__"][:200])
                    content, did_change, summary = await asyncio.to_thread(run_tool, call["name"], call["args"], frame_errors)
                    changed |= did_change
                    res = {"id": call["id"], "name": call["name"], "content": content, "summary": summary}
                except StoreError as e:
                    res = {"id": call["id"], "name": call["name"], "content": f"Error: {e}", "is_error": True,
                           "summary": str(e)}
                results.append(res)
                yield {"type": "tool", "name": call["name"], "ok": not res.get("is_error"), "summary": res["summary"]}
            with closing(store.db()) as c:
                append(c, {"role": "tool", "results": results})
            if changed:
                yield {"type": "changed"}
        else:
            yield {"type": "error", "message": f"Stopped after {MAX_ROUNDS} steps."}
        yield {"type": "done"}
