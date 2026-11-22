"""Outbound HTTP proxy for sandboxed widgets.

Widgets cannot reach the network themselves (their frame CSP has connect-src 'none').
Every request goes through here, and is checked against the hosts and secrets the
user granted to that widget instance when adding it.
"""

import asyncio
import base64
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit

import httpx

SECRET_RE = re.compile(r"\{\{\s*secret\.([A-Z][A-Z0-9_]{0,63})\s*\}\}")
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
BLOCKED_REQ_HEADERS = {"host", "content-length", "connection", "transfer-encoding", "cookie", "upgrade"}
PASS_RESP_HEADERS = {"content-type", "etag", "last-modified", "cache-control", "date", "x-ratelimit-remaining"}
MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
TIMEOUT = httpx.Timeout(10.0)
# Names that legitimately resolve to LAN addresses on a home network.
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain")


class ProxyError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail


def _split_pattern(p: str):
    """'api.x.com' | '*.x.com' | '192.168.1.5:8080' | '[::1]:80' -> (host, port|None)."""
    p = p.strip().lower()
    if p.startswith("["):
        host, _, rest = p[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else None
    if p.count(":") == 1:
        host, port = p.split(":")
        return host, int(port)
    return p, None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def match_host(host: str, port: int, patterns: list[str]):
    """Return the matching grant pattern host, or None. Unported patterns allow only 80/443."""
    for pat in patterns:
        phost, pport = _split_pattern(pat)
        if pport is None and port not in (80, 443):
            continue
        if pport is not None and pport != port:
            continue
        if phost.startswith("*."):
            if host.endswith(phost[1:]) and host != phost[2:]:
                return phost
        elif host == phost:
            return phost
    return None


async def _check_resolution(host: str, port: int, matched: str):
    """Block DNS tricks: a public name must not resolve to a private/loopback address."""
    explicit_loopback = matched in ("localhost",) or (_is_ip(matched) and ipaddress.ip_address(matched).is_loopback)
    local_name = matched == "localhost" or _is_ip(matched) or "." not in matched or matched.endswith(LOCAL_SUFFIXES)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ProxyError(502, f"cannot resolve {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_unspecified or ip.is_multicast or (ip.is_link_local and not local_name):
            raise ProxyError(403, f"{host} resolves to a disallowed address")
        if ip.is_loopback and not explicit_loopback:
            raise ProxyError(403, f"{host} resolves to loopback; grant it explicitly")
        if not ip.is_global and not ip.is_loopback and not local_name:
            raise ProxyError(403, f"{host} is a public name resolving to a private address")


async def _check_url(url: str, hosts: list[str]):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ProxyError(400, "only http(s) URLs are allowed")
    if not parts.hostname:
        raise ProxyError(400, "URL has no host")
    if parts.username or parts.password:
        raise ProxyError(400, "credentials in URL are not allowed")
    host = parts.hostname.lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    matched = match_host(host, port, hosts)
    if not matched:
        raise ProxyError(403, f"host {host}:{port} is not in this widget's granted hosts")
    await _check_resolution(host, port, matched)
    return host


def substitute(text: str, granted: list[str], values: dict[str, str], used: dict[str, str]) -> str:
    def repl(m):
        name = m.group(1)
        if name not in granted:
            raise ProxyError(403, f"secret {name} is not granted to this widget")
        if name not in values:
            raise ProxyError(424, f"secret {name} is not set; add it under Secrets")
        used[name] = values[name]
        return values[name]

    return SECRET_RE.sub(repl, text)


async def proxy_request(req: dict, grants: dict, secret_values: dict[str, str]) -> dict:
    hosts, granted_secrets = grants.get("hosts", []), grants.get("secrets", [])
    method = str(req.get("method") or "GET").upper()
    if method not in METHODS:
        raise ProxyError(400, f"method {method} not allowed")
    used: dict[str, str] = {}
    url = substitute(str(req.get("url", "")), granted_secrets, secret_values, used)
    headers = {}
    for k, v in (req.get("headers") or {}).items():
        if str(k).lower() in BLOCKED_REQ_HEADERS:
            continue
        headers[str(k)] = substitute(str(v), granted_secrets, secret_values, used)
    body = req.get("body")
    if body is not None:
        body = substitute(str(body), granted_secrets, secret_values, used).encode()

    host = await _check_url(url, hosts)
    origin_host = host
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream(method, url, headers=headers, content=body) as r:
                if r.is_redirect and "location" in r.headers:
                    url = urljoin(url, r.headers["location"])
                    host = await _check_url(url, hosts)
                    if r.status_code in (301, 302, 303):
                        method, body = ("GET" if method != "HEAD" else "HEAD"), None
                    if host != origin_host:
                        headers = {}  # never forward (possibly secret-bearing) headers cross-host
                    continue
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ProxyError(502, "upstream response exceeds 2 MB")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                return _package(r, raw, used)
    raise ProxyError(502, "too many redirects")


def _package(r: httpx.Response, raw: bytes, used: dict[str, str]) -> dict:
    ctype = r.headers.get("content-type", "")
    out = {
        "status": r.status_code,
        "headers": {k: v for k, v in r.headers.items() if k.lower() in PASS_RESP_HEADERS},
    }
    textual = not ctype or any(t in ctype for t in ("text/", "json", "xml", "javascript", "csv"))
    if textual:
        text = raw.decode(r.encoding or "utf-8", errors="replace")
        for v in used.values():  # don't let echo endpoints hand a secret back to the widget
            if len(v) >= 4:
                text = text.replace(v, "[redacted]")
        out["body"] = text
    else:
        out["bodyBase64"] = base64.b64encode(raw).decode()
    return out
