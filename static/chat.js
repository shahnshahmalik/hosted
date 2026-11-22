import { h } from "preact";
import { useState, useEffect, useRef } from "preact/hooks";
import htm from "htm";
import { api, streamPost } from "/api.js";

const html = htm.bind(h);

const WORKING = {
  get_dashboard: "Looking at the dashboard…",
  get_widget_code: "Reading widget code…",
  save_widget_type: "Writing widget code…",
  delete_widget_type: "Deleting widget type…",
  add_widget: "Adding widget…",
  update_widget: "Updating widget…",
  arrange_widgets: "Rearranging…",
  remove_widget: "Removing widget…",
  set_theme: "Changing theme…",
};
const PROVIDERS = {
  anthropic: { label: "Anthropic", model: "claude-opus-5-5", key: true },
  openai: { label: "OpenAI", model: "model name, e.g. gpt-5", key: true, url: "https://api.openai.com/v1" },
  ollama: { label: "Ollama", model: "model name, e.g. qwen3", key: false, url: "http://localhost:11434/v1" },
};

export function Chat({ frameErrors, onChanged, onClose }) {
  const [msgs, setMsgs] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [config, setConfig] = useState(null);
  const [showSettings, setShowSettings] = useState(false);
  const listRef = useRef();
  const abort = useRef(null);

  useEffect(() => {
    api("/chat").then(setMsgs);
    api("/ai/config").then((c) => {
      setConfig(c);
      if (!c.has_key && PROVIDERS[c.provider].key) setShowSettings(true);
    });
    return () => abort.current?.abort();
  }, []);
  useEffect(() => { listRef.current && (listRef.current.scrollTop = listRef.current.scrollHeight); }, [msgs, status]);

  const send = async (e) => {
    e?.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setBusy(true);
    setStatus("Thinking…");
    let list = [...msgs, { role: "user", text }];
    let live = null; // index of the assistant bubble receiving streamed text
    const push = (m) => { list = [...list, m]; setMsgs(list); return list.length - 1; };
    setMsgs(list);
    abort.current = new AbortController();
    try {
      await streamPost("/chat", { message: text, frame_errors: frameErrors() }, (ev) => {
        if (ev.type === "text") {
          if (live === null) live = push({ role: "assistant", text: "" });
          list = list.map((m, i) => (i === live ? { ...m, text: m.text + ev.text } : m));
          setMsgs(list);
          setStatus("");
        } else if (ev.type === "tool_start") {
          live = null;
          setStatus(WORKING[ev.name] || "Working…");
        } else if (ev.type === "tool") {
          live = null;
          push({ role: "tool", name: ev.name, ok: ev.ok, summary: ev.summary });
          setStatus("Thinking…");
        } else if (ev.type === "changed") {
          onChanged();
        } else if (ev.type === "error") {
          push({ role: "error", text: ev.message });
        } else if (ev.type === "retry") {
          setStatus("Retrying…");
        }
      }, abort.current.signal);
    } catch (err) {
      if (err.name !== "AbortError") push({ role: "error", text: err.message });
    } finally {
      setBusy(false);
      setStatus("");
      onChanged();
    }
  };

  const newChat = async () => {
    if (busy) return;
    await api("/chat", "DELETE");
    setMsgs([]);
  };

  return html`
    <aside class="chat framed" aria-label="Assistant">
      <div class="chat-head">
        <b>Assistant</b>
        <span class="muted small">${config ? `${PROVIDERS[config.provider].label} · ${config.model || "no model"}` : ""}</span>
        <span class="spacer"></span>
        <button class="ghost" title="New chat" onClick=${newChat} disabled=${busy}>New</button>
        <button class="ghost" title="AI settings" onClick=${() => setShowSettings(!showSettings)}>⚙</button>
        <button class="ghost" title="Close" onClick=${onClose}>✕</button>
      </div>
      ${showSettings && config && html`<${AISettings} config=${config}
          onSaved=${(c) => { setConfig(c); setShowSettings(false); }} />`}
      <div class="chat-list" ref=${listRef}>
        ${msgs.length === 0 && !showSettings && html`<div class="muted small chat-empty">
          Ask for widgets, layouts or themes. For example:<br />
          “Add a clock for Tokyo next to the weather”<br />
          “Make a widget showing the Bitcoin price in euros”<br />
          “Switch to a warm light theme”</div>`}
        ${msgs.map((m) =>
          m.role === "tool"
            ? html`<div class=${"chat-tool" + (m.ok ? "" : " failed")}>${m.ok ? "✓" : "✕"} ${m.summary}</div>`
            : html`<div class=${"chat-msg " + m.role}>${m.text}</div>`)}
        ${status && html`<div class="chat-status muted small">${status}</div>`}
      </div>
      <form class="chat-input" onSubmit=${send}>
        <textarea rows="2" placeholder="Ask the assistant…" value=${input} disabled=${busy}
          onInput=${(e) => setInput(e.currentTarget.value)}
          onKeyDown=${(e) => { if (e.key === "Enter" && !e.shiftKey) send(e); }}></textarea>
        ${busy
          ? html`<button type="button" class="ghost" onClick=${() => abort.current?.abort()}>Stop</button>`
          : html`<button type="submit" disabled=${!input.trim()}>Send</button>`}
      </form>
    </aside>`;
}

function AISettings({ config, onSaved }) {
  const [provider, setProvider] = useState(config.provider);
  const [model, setModel] = useState(config.model);
  const [baseUrl, setBaseUrl] = useState(config.base_url || "");
  const [effort, setEffort] = useState(config.effort);
  const [key, setKey] = useState("");
  const [err, setErr] = useState(null);
  const p = PROVIDERS[provider];

  const switchProvider = (np) => {
    setProvider(np);
    setModel(np === "anthropic" ? PROVIDERS.anthropic.model : "");
    setBaseUrl(PROVIDERS[np].url || "");
  };
  const save = async (e) => {
    e.preventDefault();
    try {
      const body = { provider, model, base_url: p.url ? baseUrl : null, effort };
      if (key) body.api_key = key;
      onSaved(await api("/ai/config", "PUT", body));
    } catch (x) { setErr(x.message); }
  };
  const forget = async () => onSaved(await api("/ai/config", "PUT", { provider, model, base_url: baseUrl || null, effort, api_key: "" }));

  return html`<form class="ai-settings" onSubmit=${save}>
    <label>Provider<select value=${provider} onChange=${(e) => switchProvider(e.currentTarget.value)}>
      ${Object.entries(PROVIDERS).map(([k, v]) => html`<option value=${k}>${v.label}</option>`)}</select></label>
    <label>Model<input value=${model} placeholder=${p.model} onInput=${(e) => setModel(e.currentTarget.value)} /></label>
    ${p.url && html`<label>Base URL<input value=${baseUrl} placeholder=${p.url} onInput=${(e) => setBaseUrl(e.currentTarget.value)} /></label>`}
    ${provider === "anthropic" && html`<label>Effort<select value=${effort} onChange=${(e) => setEffort(e.currentTarget.value)}>
      ${["low", "medium", "high", "xhigh"].map((x) => html`<option value=${x}>${x}</option>`)}</select></label>`}
    ${p.key && html`<label>API key
      <input type="password" autocomplete="off" value=${key} onInput=${(e) => setKey(e.currentTarget.value)}
        placeholder=${provider === config.provider && config.has_key ? "saved — leave blank to keep" : "paste your key"} /></label>`}
    <div class="muted small">Your key is stored on this server only and sent only to ${p.label}. It is never shown to widgets.</div>
    ${err && html`<div class="error small">${err}</div>`}
    <div class="row"><button type="submit">Save</button>
      ${p.key && provider === config.provider && config.has_key && html`<button type="button" class="ghost" onClick=${forget}>Forget key</button>`}</div>
  </form>`;
}
