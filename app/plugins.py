"""Install widget packages from GitHub.

A package is a folder with manifest.json + index.html (the same layout as widgets/<id>/).
A URL may point at one package or at a folder/repo containing several (a collection).

Everything is pinned to a commit SHA: preview shows what is at a commit, install fetches
exactly that commit, so what the user reviewed is what gets installed. The server only ever
talks to api.github.com and raw.githubusercontent.com; the URL is parsed into owner/repo/ref/
path, never fetched directly.
"""

import json
import os
import re
from contextlib import closing
from urllib.parse import quote

import httpx

from . import store
from .store import StoreError

API = os.environ.get("LABCANVAS_GITHUB_API") or os.environ.get("HOSTED_GITHUB_API", "https://api.github.com")
RAW = os.environ.get("LABCANVAS_GITHUB_RAW") or os.environ.get("HOSTED_GITHUB_RAW", "https://raw.githubusercontent.com")
MAX_MANIFEST = 64 * 1024
MAX_PACKAGES = 50
MAX_DEPTH = 3  # how deep below the given folder to look for packages

URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100}?)(?:\.git)?"
    r"(?:/(?:tree|blob)/([A-Za-z0-9._-]{1,100})(/[^?#]*)?)?/?(?:[?#].*)?$"
)
SEGMENT_RE = re.compile(r"^[A-Za-z0-9._ -]{1,100}$")


def parse_url(url: str) -> dict:
    """github.com/owner/repo[/tree|blob/<ref>[/path]] -> {owner, repo, ref|None, path}."""
    m = URL_RE.match((url or "").strip())
    if not m:
        raise StoreError("Use a GitHub URL like https://github.com/owner/repo or "
                         "https://github.com/owner/repo/tree/main/path/to/widget")
    owner, repo, ref, path = m.group(1), m.group(2), m.group(3), (m.group(4) or "").strip("/")
    parts = [p for p in path.split("/") if p]
    if any(p in (".", "..") or not SEGMENT_RE.match(p) for p in parts):
        raise StoreError("invalid path in URL")
    if parts and parts[-1] in ("manifest.json", "index.html"):  # a blob link to a package file
        parts = parts[:-1]
    return {"owner": owner, "repo": repo, "ref": ref, "path": "/".join(parts)}


def _headers(token):
    h = {"Accept": "application/vnd.github+json", "User-Agent": "labcanvas"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _token():
    with closing(store.db()) as c:
        r = c.execute("SELECT value FROM secrets WHERE name='GITHUB_TOKEN'").fetchone()
        return r["value"] if r else None


async def _get(client, url, token, what):
    try:
        r = await client.get(url, headers=_headers(token))
    except httpx.HTTPError as e:
        raise StoreError(f"could not reach GitHub ({type(e).__name__})")
    if r.status_code == 404 or (r.status_code == 422 and "/commits/" in url):
        hint = "" if token else " Private repositories need a GITHUB_TOKEN secret (Secrets in the header)."
        raise StoreError(f"{what} not found.{hint}")
    if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
        raise StoreError("GitHub rate limit reached; try again later or add a GITHUB_TOKEN secret.")
    if r.status_code == 401:
        raise StoreError("GitHub rejected the GITHUB_TOKEN secret.")
    if r.status_code >= 400:
        raise StoreError(f"GitHub error {r.status_code} for {what}")
    return r


async def resolve(client, owner, repo, ref, token) -> tuple[str, str]:
    """Return (ref, commit sha). No ref = the repository's default branch."""
    base = f"{API}/repos/{owner}/{repo}"
    if not ref:
        ref = (await _get(client, base, token, f"repository {owner}/{repo}")).json()["default_branch"]
    data = (await _get(client, f"{base}/commits/{quote(ref, safe='')}", token, f"ref {ref!r} in {owner}/{repo}")).json()
    sha = data.get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise StoreError("unexpected response from GitHub")
    return ref, sha


async def discover(client, owner, repo, sha, path, token) -> list[str]:
    """Folders at or below `path` (up to MAX_DEPTH levels) that contain manifest.json and index.html."""
    tree = (await _get(client, f"{API}/repos/{owner}/{repo}/git/trees/{sha}?recursive=1", token, "repository tree")).json()
    files = {t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"}
    prefix = f"{path}/" if path else ""
    found = []
    for f in sorted(files):
        if not f.endswith("manifest.json") or not f.startswith(prefix):
            continue
        folder = f[: -len("manifest.json")].rstrip("/")
        rel = folder[len(prefix):] if prefix else folder
        if rel.count("/") + (1 if rel else 0) > MAX_DEPTH:
            continue
        if f"{folder}/index.html" in files or (folder == "" and "index.html" in files):
            found.append(folder)
    if not found:
        trunc = " (the repository is too large to scan; link directly to a widget folder)" if tree.get("truncated") else ""
        raise StoreError(f"no widget packages (manifest.json + index.html) found at this location{trunc}")
    return found[:MAX_PACKAGES]


async def fetch_package(client, owner, repo, sha, folder, token) -> tuple[dict, str]:
    def raw(name):
        p = f"{folder}/{name}" if folder else name
        return f"{RAW}/{owner}/{repo}/{sha}/{quote(p)}"

    mr = await _get(client, raw("manifest.json"), token, f"{folder or '/'}/manifest.json")
    if len(mr.content) > MAX_MANIFEST:
        raise StoreError("manifest.json is too large")
    try:
        manifest = json.loads(mr.text)
    except ValueError:
        raise StoreError(f"{folder or '/'}/manifest.json is not valid JSON")
    cr = await _get(client, raw("index.html"), token, f"{folder or '/'}/index.html")
    if len(cr.content) > store.MAX_CODE:
        raise StoreError("index.html exceeds 200 KB")
    return manifest, cr.text


# ---------- ids and origins ----------

def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    if not s or not s[0].isalpha():
        s = "w-" + s
    return s[:41].rstrip("-")


def _origin_key(o: dict) -> tuple:
    return (o.get("owner", "").lower(), o.get("repo", "").lower(), o.get("path", ""))


def find_installed(c, owner, repo, path):
    for d in store.list_defs(c):
        if d["source"] == "installed" and isinstance(d.get("origin"), dict) and \
                _origin_key(d["origin"]) == (owner.lower(), repo.lower(), path):
            return d
    return None


def choose_id(c, manifest: dict, owner: str, repo: str, folder: str) -> str:
    """Prefer the manifest's id, then the folder or repo name; prefix with the owner on collision."""
    base = _slug(str(manifest.get("id") or (folder.split("/")[-1] if folder else repo)))
    for cand in (base, _slug(f"{owner}-{base}")):
        if not store.get_def(c, cand, with_code=False):
            return cand
    n = 2
    while store.get_def(c, f"{base[:37]}-{n}", with_code=False):
        n += 1
    return f"{base[:37]}-{n}"


# ---------- operations ----------

async def preview(url: str) -> dict:
    loc = parse_url(url)
    token = _token()
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        ref, sha = await resolve(client, loc["owner"], loc["repo"], loc["ref"], token)
        folders = await discover(client, loc["owner"], loc["repo"], sha, loc["path"], token)
        packages = []
        for folder in folders:
            item = {"path": folder, "source_url": f"https://github.com/{loc['owner']}/{loc['repo']}/tree/{sha}/{folder}".rstrip("/")}
            try:
                manifest, code = await fetch_package(client, loc["owner"], loc["repo"], sha, folder, token)
                with closing(store.db()) as c:
                    existing = find_installed(c, loc["owner"], loc["repo"], folder)
                    type_id = existing["id"] if existing else choose_id(c, manifest, loc["owner"], loc["repo"], folder)
                m = store.validate_manifest(manifest, type_id)
                item.update(manifest=m, code_bytes=len(code.encode()), type_id=type_id)
                if existing:
                    item["installed"] = {"sha": existing["origin"].get("sha"), "permissions": existing["permissions"]}
            except StoreError as e:
                item["error"] = str(e)
            packages.append(item)
    return {"owner": loc["owner"], "repo": loc["repo"], "ref": ref, "sha": sha, "packages": packages}


async def install(url: str, sha: str, folder: str) -> dict:
    """Install (or update) the package at `folder` exactly as it is at commit `sha`."""
    loc = parse_url(url)
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        raise StoreError("install needs the commit sha shown in the preview")
    folder = (folder or "").strip("/")
    if any(p in (".", "..") or not SEGMENT_RE.match(p) for p in folder.split("/") if p):
        raise StoreError("invalid package path")
    token = _token()
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        manifest, code = await fetch_package(client, loc["owner"], loc["repo"], sha, folder, token)
    with closing(store.db()) as c:
        existing = find_installed(c, loc["owner"], loc["repo"], folder)
        type_id = existing["id"] if existing else choose_id(c, manifest, loc["owner"], loc["repo"], folder)
        origin = {"owner": loc["owner"], "repo": loc["repo"], "path": folder, "sha": sha,
                  # keep tracking the branch/tag the user installed from, for update checks
                  "ref": loc["ref"] or (existing["origin"].get("ref") if existing else None)}
        d = store.save_def(c, type_id, manifest, code, source="installed", origin=json.dumps(origin))
        users = [w for w in store.list_widgets(c) if w["config"].get("type") == type_id]
        d["instances_awaiting_permission"] = [w["id"] for w in users if any(w["pending"].values())]
        d["updated"] = bool(existing)
        return d


async def check_updates() -> list[dict]:
    """For each installed type, compare its commit with the latest commit of the ref it tracks."""
    with closing(store.db()) as c:
        installed = [d for d in store.list_defs(c) if d["source"] == "installed" and isinstance(d.get("origin"), dict)]
    token, out, latest = _token(), [], {}
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        for d in installed:
            o = d["origin"]
            key = (o["owner"], o["repo"], o.get("ref"))
            try:
                if key not in latest:
                    latest[key] = await resolve(client, o["owner"], o["repo"], o.get("ref"), token)
                ref, sha = latest[key]
                if sha != o.get("sha"):
                    out.append({"id": d["id"], "ref": ref, "current": o.get("sha"), "latest": sha})
            except StoreError as e:
                out.append({"id": d["id"], "error": str(e)})
    return out


async def update(type_id: str) -> dict:
    with closing(store.db()) as c:
        d = store.get_def(c, type_id, with_code=False)
    if not d or d["source"] != "installed" or not isinstance(d.get("origin"), dict):
        raise StoreError(f"{type_id!r} is not an installed widget")
    o = d["origin"]
    token = _token()
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        ref, sha = await resolve(client, o["owner"], o["repo"], o.get("ref"), token)
    url = f"https://github.com/{o['owner']}/{o['repo']}" + (f"/tree/{o['ref']}" if o.get("ref") else "")
    return await install(url, sha, o.get("path", ""))
