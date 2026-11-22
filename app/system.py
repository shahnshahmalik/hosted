"""Host system stats for widgets with the "system" capability, and URL status checks for "ping".

One snapshot is cached briefly so several live widgets polling every couple of seconds cost a
single psutil sweep. In Docker, CPU/memory/temperature are the host's (via /proc and /sys);
disks are whatever is mounted into the container (see README / HOSTED_DISK_PATHS).
"""

import asyncio
import glob
import os
import platform
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import psutil

CACHE_SECONDS = 1.0
SKIP_FS = {"squashfs", "tmpfs", "devtmpfs", "autofs", "proc", "sysfs", "cgroup", "cgroup2", "devfs", "nullfs"}
SKIP_MOUNT_PREFIXES = ("/System/Volumes/", "/snap/", "/var/lib/docker/", "/run/", "/dev")
CPU_SENSOR_KEYS = ("cpu_thermal", "cpu-thermal", "coretemp", "k10temp", "soc_thermal", "cpu", "acpitz")

_cache = {"t": 0.0, "data": None}
# Concurrent polls must not sample twice: a second cpu_percent() call milliseconds after the first reads ~0%.
_lock = threading.Lock()
# psutil keeps cpu_percent() state per thread, so every sample must come from the same thread.
EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hosted-system")
# Prime on that thread: the first non-blocking reading is otherwise 0.
EXECUTOR.submit(lambda: (psutil.cpu_percent(None), psutil.cpu_percent(None, percpu=True)))
_io = {"t": 0.0, "read": 0, "write": 0}


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip().strip("\x00")
    except OSError:
        return None


def _os_name():
    for line in (_read("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    if platform.system() == "Darwin":
        return f"macOS {platform.mac_ver()[0]}"
    return f"{platform.system()} {platform.release()}"


def _temps():
    sensors = []
    if hasattr(psutil, "sensors_temperatures"):
        try:
            for name, entries in psutil.sensors_temperatures().items():
                for e in entries:
                    sensors.append({"name": name, "label": e.label or name, "current": round(e.current, 1),
                                    "high": e.high, "critical": e.critical})
        except Exception:
            pass
    if not sensors:  # fallback: thermal zones (e.g. Raspberry Pi without lm-sensors)
        for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            t, kind = _read(f"{zone}/temp"), _read(f"{zone}/type") or "zone"
            if t and t.lstrip("-").isdigit():
                sensors.append({"name": kind, "label": kind, "current": round(int(t) / 1000, 1), "high": None, "critical": None})
    cpu = next((s for key in CPU_SENSOR_KEYS for s in sensors if s["name"].lower().startswith(key)), None)
    cpu = cpu or (sensors[0] if sensors else None)
    return {"cpu": cpu["current"] if cpu else None, "cpu_high": cpu and (cpu["high"] or cpu["critical"]), "sensors": sensors[:20]}


def _disks():
    wanted = [p for p in os.environ.get("HOSTED_DISK_PATHS", "").split(",") if p.strip()]
    seen, out = set(), []
    parts = psutil.disk_partitions(all=False)
    mounts = wanted or [p.mountpoint for p in parts
                        if p.fstype not in SKIP_FS and not p.mountpoint.startswith(SKIP_MOUNT_PREFIXES)]
    by_mount = {p.mountpoint: p for p in parts}
    for m in mounts:
        try:
            u = psutil.disk_usage(m)
        except OSError:
            continue
        p = by_mount.get(m)
        key = (p.device if p else m, u.total)
        if key in seen or u.total == 0:
            continue
        seen.add(key)
        out.append({"mount": m, "device": p.device if p else "", "fstype": p.fstype if p else "",
                    "total": u.total, "used": u.used, "free": u.free, "percent": u.percent})
    io = {"read_bps": None, "write_bps": None}
    try:
        c, now = psutil.disk_io_counters(), time.time()
        if c and _io["t"]:
            dt = max(now - _io["t"], 0.001)
            io = {"read_bps": max(0, (c.read_bytes - _io["read"]) / dt), "write_bps": max(0, (c.write_bytes - _io["write"]) / dt)}
        if c:
            _io.update(t=now, read=c.read_bytes, write=c.write_bytes)
    except Exception:
        pass
    return {"disks": out[:16], "io": io}


def _ips():
    ips = []
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith("127."):
                ips.append({"interface": name, "address": a.address})
    return ips[:8]


def snapshot() -> dict:
    with _lock:
        now = time.time()
        if _cache["data"] and now - _cache["t"] < CACHE_SECONDS:
            return _cache["data"]
        return _sample(now)


def _sample(now: float) -> dict:
    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    freq = None
    try:
        f = psutil.cpu_freq()
        freq = round(f.current) if f and f.current else None
    except Exception:
        pass
    boot = psutil.boot_time()
    data = {
        "time": now,
        "cpu": {"percent": psutil.cpu_percent(None), "per_core": psutil.cpu_percent(None, percpu=True),
                "cores": psutil.cpu_count(), "physical_cores": psutil.cpu_count(False), "freq_mhz": freq,
                "load": [round(x, 2) for x in os.getloadavg()] if hasattr(os, "getloadavg") else None,
                "model": _cpu_model()},
        "memory": {"total": vm.total, "used": vm.total - vm.available, "available": vm.available, "percent": vm.percent,
                   "swap_total": sw.total, "swap_used": sw.used, "swap_percent": sw.percent},
        **_disks(),
        "temps": _temps(),
        "info": {"hostname": socket.gethostname(), "os": _os_name(), "kernel": platform.release(),
                 "arch": platform.machine(), "model": _read("/proc/device-tree/model") or _read("/sys/devices/virtual/dmi/id/product_name"),
                 "boot_time": boot, "uptime": int(now - boot), "processes": len(psutil.pids()), "ips": _ips()},
    }
    _cache.update(t=now, data=data)
    return data


_model_cache = {}


def _cpu_model():
    if "m" not in _model_cache:
        model = None
        for line in (_read("/proc/cpuinfo") or "").splitlines():
            if line.lower().startswith(("model name", "hardware", "cpu model")):
                model = line.split(":", 1)[1].strip()
                break
        _model_cache["m"] = model or platform.processor() or None
    return _model_cache["m"]


# ---------- ping ----------

async def ping(urls: list[str]) -> list[dict]:
    """Reachability only: status code and latency, never the response body."""
    async def one(client, url):
        start = time.perf_counter()
        try:
            async with client.stream("GET", url) as r:  # headers only; the body is never read
                return {"url": url, "ok": r.status_code < 500, "status": r.status_code,
                        "ms": round((time.perf_counter() - start) * 1000)}
        except Exception as e:
            return {"url": url, "ok": False, "status": None, "error": type(e).__name__}

    # Self-signed certificates are normal on home networks; only reachability is reported.
    async with httpx.AsyncClient(timeout=4.0, verify=False, follow_redirects=False) as client:
        return await asyncio.gather(*(one(client, u) for u in urls))
