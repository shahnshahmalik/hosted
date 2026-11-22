export const api = async (path, method = "GET", body) => {
  const r = await fetch("/api" + path, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let detail = `${method} ${path} failed: ${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return r.status === 204 ? null : r.json();
};

/** POST and read a text/event-stream response, calling onEvent for each `data:` JSON payload. */
export async function streamPost(path, body, onEvent, signal) {
  const r = await fetch("/api" + path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal,
  });
  if (!r.ok) {
    let detail = `request failed: ${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      for (const line of chunk.split("\n"))
        if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)));
    }
  }
}
