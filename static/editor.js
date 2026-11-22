// Widget editor: code (left 70%) + AI chat (right 30%), with a sandboxed preview of the draft.
import { h } from "preact";
import { useState, useEffect, useRef } from "preact/hooks";
import htm from "htm";
import { api, streamPost } from "/api.js";

const html = htm.bind(h);
const CELL_W = 106, CELL_H = 80, GAP = 12; // a typical dashboard cell, for previewing at real size

const draftKey = (wid) => `hosted.draft.${wid}`;
const pretty = (o) => JSON.stringify(o, null, 2);

export function Editor({ wid }) {
  const [src, setSrc] = useState(null);        // what the server has
  const [code, setCode] = useState("");
  const [manifestText, setManifestText] = useState("");
  const [tab, setTab] = useState("code");      // code | manifest
  const [view, setView] = useState("edit");    // edit | preview
  const [status, setStatus] = useState(null);  // {kind: ok|error|info, text}
  const [restored, setRestored] = useState(false);
  const [saving, setSaving] = useState(false);
  const [preview, setPreview] = useState(null); // {url, key}
  const [previewErrors, setPreviewErrors] = useState([]);
  const [theme, setTheme] = useState({});
  const [secretNames, setSecretNames] = useState([]);
  const frameRef = useRef(null);
  const loads = useRef({});

  const dirty = !!src && (code !== src.code || manifestText !== pretty(src.manifest));
  const parsedManifest = () => {
    try { return { ok: true, value: JSON.parse(manifestText) }; }
    catch (e) { return { ok: false, error: `manifest.json: ${e.message}` }; }
  };

  // ---------- load ----------
  const load = async (keepDraft = true) => {
    const [s, t, sn] = await Promise.all([api(`/widgets/${wid}/source`), api("/theme"), api("/secrets")]);
    setSrc(s); setTheme(t.vars || {}); setSecretNames(sn);
    let c = s.code, m = pretty(s.manifest);
    const local = keepDraft && JSON.parse(localStorage.getItem(draftKey(wid)) || "null");
    if (local && local.revision === s.revision && local.type === s.type && (local.code !== c || local.manifestText !== m)) {
      c = local.code; m = local.manifestText; setRestored(true);
    }
    setCode(c); setManifestText(m);
  };
  useEffect(() => { load().catch((e) => setStatus({ kind: "error", text: e.message })); }, [wid]);

  // keep an unsaved draft in this browser so a reload doesn't lose work
  useEffect(() => {
    if (!src) return;
    if (dirty) localStorage.setItem(draftKey(wid), JSON.stringify({ code, manifestText, revision: src.revision, type: src.type }));
    else localStorage.removeItem(draftKey(wid));
  }, [code, manifestText, src]);
  useEffect(() => {
    const warn = (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } };
    addEventListener("beforeunload", warn);
    return () => removeEventListener("beforeunload", warn);
  }, [dirty]);

  // ---------- preview + SDK bridge ----------
  const runPreview = async () => {
    setPreviewErrors([]);
    try {
      const { url } = await api(`/widgets/${wid}/preview`, "POST", { code });
      setPreview({ url, key: url });
      setView("preview");
    } catch (e) { setStatus({ kind: "error", text: e.message }); }
  };

  useEffect(() => {
    if (!src) return;
    const w = src.widget;
    const post = (msg) => frameRef.current?.contentWindow?.postMessage({ hosted: 1, ...msg }, "*");
    const handlers = {
      fetch: (p) => api(`/widgets/${wid}/proxy`, "POST", p),
      "storage.get": async (p) => (await api(`/widgets/${wid}/storage/${encodeURIComponent(p.key)}`)).value,
      "storage.set": (p) => api(`/widgets/${wid}/storage/${encodeURIComponent(p.key)}`, "PUT", { value: p.value ?? null }),
      "storage.remove": (p) => api(`/widgets/${wid}/storage/${encodeURIComponent(p.key)}`, "DELETE"),
      system: () => api(`/widgets/${wid}/system`),
      ping: (p) => api(`/widgets/${wid}/ping`, "POST", { urls: (p.urls || []).slice(0, 40) }),
      open: async (p) => {
        const url = new URL(p.url);
        if (!["http:", "https:"].includes(url.protocol)) throw new Error("only http(s) links can be opened");
        if (!navigator.userActivation?.isActive) throw new Error("links can only be opened from a click");
        window.open(url.href, "_blank", "noopener,noreferrer");
        return null;
      },
      "props.set": async () => null, // settings changes aren't saved from a preview
    };
    const onMessage = async (e) => {
      const m = e.data;
      if (!m || m.hosted !== 1 || !frameRef.current || e.source !== frameRef.current.contentWindow) return;
      if (m.type === "hello") {
        const pm = parsedManifest();
        const defaults = Object.fromEntries(Object.entries((pm.ok && pm.value.props) || {}).map(([k, p]) => [k, p?.default]));
        return post({ type: "init", payload: {
          id: wid, props: { ...defaults, ...(w.config.props || {}) }, theme,
          secrets: (w.grants?.secrets || []).filter((n) => secretNames.includes(n)),
        } });
      }
      if (m.type === "error") return setPreviewErrors((s) => (s.includes(m.payload?.message) ? s : [...s, String(m.payload?.message)].slice(-10)));
      const fn = handlers[m.type];
      if (!fn) return post({ type: "reply", id: m.id, ok: false, error: `unknown call ${m.type}` });
      try { post({ type: "reply", id: m.id, ok: true, result: await fn(m.payload || {}) }); }
      catch (err) { post({ type: "reply", id: m.id, ok: false, error: err.message }); }
    };
    addEventListener("message", onMessage);
    return () => removeEventListener("message", onMessage);
  }, [src, theme, secretNames, manifestText]);

  const onFrameLoad = (key, el) => {
    loads.current[key] = (loads.current[key] || 0) + 1;
    if (loads.current[key] > 1) { el.src = "about:blank"; setPreviewErrors((s) => [...s, "Stopped: the widget tried to navigate away from its frame."]); }
  };

  // ---------- save / revert ----------
  const save = async () => {
    const pm = parsedManifest();
    if (!pm.ok) { setTab("manifest"); return setStatus({ kind: "error", text: pm.error }); }
    setSaving(true); setStatus(null);
    try {
      const r = await api(`/widgets/${wid}/source`, "PUT", { manifest: pm.value, code });
      localStorage.removeItem(draftKey(wid));
      await load(false);
      setRestored(false);
      setStatus({ kind: "ok", text: r.forked
        ? `Saved as your own copy “${r.def.name}”. This widget now uses it; the original is unchanged.`
        : "Saved. Widgets using this type update right away." });
    } catch (e) { setStatus({ kind: "error", text: e.message }); }
    finally { setSaving(false); }
  };
  const revert = (ask = true) => {
    if (ask && dirty && !confirm("Discard your unsaved changes?")) return;
    setCode(src.code); setManifestText(pretty(src.manifest)); setRestored(false);
    localStorage.removeItem(draftKey(wid));
  };
  const allow = async () => {
    await api(`/widgets/${wid}/grant`, "POST");
    await load(false);
    setStatus({ kind: "ok", text: "Allowed. Press Preview to try it." });
  };

  useEffect(() => {
    const onKey = (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); if (dirty && !saving) save(); } };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  });

  if (!src) return html`<div class="editor-loading muted">${status?.text || "Loading…"}</div>`;

  const pm = parsedManifest();
  const size = (pm.ok && pm.value.size) || { w: src.widget.w, h: src.widget.h };
  const pending = src.widget.pending || {};
  const needsAllow = ["hosts", "secrets", "capabilities"].some((k) => (pending[k] || []).length);

  return html`
    <div class="editor">
      <section class="editor-code framed">
        <div class="editor-bar">
          <a class="linkbtn" href="/" onClick=${(e) => { if (dirty && !confirm("Leave without saving? Your draft stays in this browser.")) e.preventDefault(); }}>← Dashboard</a>
          <div class="editor-title">
            <b>${src.widget.title}</b>
            <span class="tag">${src.type || "empty"}</span>
            <span class="muted small">${src.copy_on_save
              ? (src.source ? `${src.source} widget · saving creates your own copy` : "saving creates a widget type")
              : src.instances > 1 ? `used by ${src.instances} widgets · saving updates all of them` : "your widget type"}</span>
          </div>
          <div class="editor-actions">
            ${dirty && html`<span class="muted small">unsaved</span>`}
            <button class=${view === "preview" ? "active" : "ghost"} onClick=${() => (view === "preview" ? setView("edit") : runPreview())}>
              ${view === "preview" ? "‹ Code" : "▶ Preview"}</button>
            <button class="ghost" disabled=${!dirty} onClick=${() => revert()}>Revert</button>
            <button disabled=${!dirty || saving} onClick=${save}>${saving ? "Saving…" : "Save"}</button>
          </div>
        </div>
        ${restored && html`<div class="editor-note">Restored your unsaved draft from this browser. <button class="ghost small" onClick=${() => revert(false)}>Discard</button></div>`}
        ${status && html`<div class=${"editor-note " + status.kind}>${status.text}</div>`}
        ${needsAllow && html`<div class="allow-bar"><span>This widget needs permission: ${[...(pending.hosts || []), ...(pending.secrets || []).map((s) => "secret " + s), ...(pending.capabilities || [])].join(", ")}</span>
          <button onClick=${allow}>Allow</button></div>`}
        ${view === "edit" ? html`
          <div class="tabs editor-tabs">
            <button class=${tab === "code" ? "active" : "ghost"} onClick=${() => setTab("code")}>index.html</button>
            <button class=${tab === "manifest" ? "active" : "ghost"} onClick=${() => setTab("manifest")}>manifest.json</button>
            ${!pm.ok && html`<span class="error small">${pm.error}</span>`}
          </div>
          ${tab === "code"
            ? html`<${CodeArea} key="code" value=${code} onInput=${setCode} />`
            : html`<${CodeArea} key="manifest" value=${manifestText} onInput=${setManifestText} />`}`
        : html`
          <div class="preview-stage">
            <div class="preview-meta muted small">Preview of your draft at ${size.w}×${size.h} cells · uses this widget's settings, data and current permissions
              <button class="ghost small" onClick=${runPreview}>↻ Refresh</button></div>
            <div class="widget framed preview-card" style=${`width:${size.w * CELL_W + (size.w - 1) * GAP}px;height:${size.h * CELL_H + (size.h - 1) * GAP}px`}>
              <div class="widget-head"><span class="title">${(pm.ok && pm.value.name) || src.widget.title}</span></div>
              ${preview && html`<iframe key=${preview.key} class="widget-frame" src=${preview.url} sandbox="allow-scripts"
                 referrerpolicy="no-referrer" title="Preview" ref=${frameRef} onLoad=${(e) => onFrameLoad(preview.key, e.currentTarget)}></iframe>`}
            </div>
            ${previewErrors.length > 0 && html`<div class="preview-errors">${previewErrors.map((e) => html`<div class="error small">⚠ ${e}</div>`)}
              <div class="muted small">The assistant sees these errors with your next message.</div></div>`}
          </div>`}
      </section>
      <${EditChat} wid=${wid} getDraft=${() => ({ manifest: parsedManifest(), code })} errors=${previewErrors}
        onDraft=${(d) => { setCode(d.code); setManifestText(pretty(d.manifest)); if (view === "preview") setTimeout(runPreview, 0); }} />
    </div>`;
}

function CodeArea({ value, onInput }) {
  const ta = useRef(), gutter = useRef();
  const lines = value.split("\n").length;
  const onKeyDown = (e) => {
    if (e.key !== "Tab") return;
    e.preventDefault();
    const el = e.currentTarget, s = el.selectionStart, end = el.selectionEnd;
    const next = value.slice(0, s) + "  " + value.slice(end);
    onInput(next);
    requestAnimationFrame(() => { el.selectionStart = el.selectionEnd = s + 2; });
  };
  return html`<div class="code-area">
    <pre class="code-gutter" ref=${gutter} aria-hidden="true">${Array.from({ length: lines }, (_, i) => i + 1).join("\n")}</pre>
    <textarea ref=${ta} class="code-input" spellcheck="false" autocapitalize="off" autocomplete="off" wrap="off" value=${value}
      onInput=${(e) => onInput(e.currentTarget.value)} onKeyDown=${onKeyDown}
      onScroll=${(e) => { gutter.current.scrollTop = e.currentTarget.scrollTop; }}></textarea>
  </div>`;
}

function EditChat({ wid, getDraft, errors, onDraft }) {
  const [msgs, setMsgs] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const listRef = useRef(), abort = useRef(null);
  useEffect(() => { listRef.current && (listRef.current.scrollTop = listRef.current.scrollHeight); }, [msgs, status]);

  const send = async (e) => {
    e?.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    const d = getDraft();
    if (!d.manifest.ok) { setMsgs((m) => [...m, { role: "error", text: `Fix ${d.manifest.error} first.` }]); return; }
    const history = msgs.filter((m) => m.role === "user" || m.role === "assistant").map(({ role, text }) => ({ role, text }));
    let list = [...msgs, { role: "user", text }], live = null;
    const push = (m) => { list = [...list, m]; setMsgs(list); return list.length - 1; };
    setMsgs(list); setInput(""); setBusy(true); setStatus("Thinking…");
    abort.current = new AbortController();
    try {
      await streamPost(`/widgets/${wid}/edit-chat`, { message: text, history, manifest: d.manifest.value, code: d.code, errors }, (ev) => {
        if (ev.type === "text") {
          if (live === null) live = push({ role: "assistant", text: "" });
          list = list.map((m, i) => (i === live ? { ...m, text: m.text + ev.text } : m));
          setMsgs(list); setStatus("");
        } else if (ev.type === "tool_start") { live = null; setStatus("Editing the code…"); }
        else if (ev.type === "tool") { live = null; push({ role: "tool", ok: ev.ok, summary: ev.summary }); setStatus("Thinking…"); }
        else if (ev.type === "draft") onDraft(ev);
        else if (ev.type === "error") push({ role: "error", text: ev.message });
      }, abort.current.signal);
    } catch (err) {
      if (err.name !== "AbortError") push({ role: "error", text: err.message });
    } finally { setBusy(false); setStatus(""); }
  };

  return html`<aside class="edit-chat framed" aria-label="Edit with AI">
    <div class="chat-head"><b>Edit with AI</b><span class="spacer"></span>
      <button class="ghost" title="Clear conversation" disabled=${busy} onClick=${() => setMsgs([])}>New</button></div>
    <div class="chat-list" ref=${listRef}>
      ${msgs.length === 0 && html`<div class="muted small chat-empty">Describe the change you want. The assistant edits the code on the left; preview it, then save. For example:<br />
        “Make the numbers bigger and use the accent color”<br />
        “Add a setting to choose the refresh interval”<br />
        “Show a small chart of the last hour”</div>`}
      ${msgs.map((m) => (m.role === "tool"
        ? html`<div class=${"chat-tool" + (m.ok ? "" : " failed")}>${m.ok ? "✓" : "✕"} ${m.summary}</div>`
        : html`<div class=${"chat-msg " + m.role}>${m.text}</div>`))}
      ${status && html`<div class="chat-status muted small">${status}</div>`}
    </div>
    <form class="chat-input" onSubmit=${send}>
      <textarea rows="3" placeholder="What should change?" value=${input} disabled=${busy}
        onInput=${(e) => setInput(e.currentTarget.value)} onKeyDown=${(e) => { if (e.key === "Enter" && !e.shiftKey) send(e); }}></textarea>
      ${busy ? html`<button type="button" class="ghost" onClick=${() => abort.current?.abort()}>Stop</button>`
             : html`<button type="submit" disabled=${!input.trim()}>Send</button>`}
    </form>
  </aside>`;
}
