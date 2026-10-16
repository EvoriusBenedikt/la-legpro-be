"""Provenance for the hardware metrics (engineer KPIs + system_metrics loop).

Both producers must measure the hardware the stack actually runs on. Inside a
container '/' is the image overlay or the desktop VM's virtual disk — a
phantom volume that on the dev stack hid the real, nearly full host drive
behind a calm 7% (bug 2026-10-12). The app's bytes live on the ./data bind
mount, so that filesystem is the disk worth watching; on Linux production it
is the data volume, equally the operationally correct choice.

CPU/RAM stay runtime-wide (psutil inside a container sees the container
runtime: the Docker Desktop VM on dev, the host kernel on Linux production);
RUNTIME_SCOPE lets the UI say so instead of misattributing them.
"""
import os

import psutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")


def _detect_runtime_scope() -> str:
    if os.path.exists("/.dockerenv"):
        return "container"
    try:
        with open("/proc/1/cgroup", encoding="utf-8") as fh:
            cgroups = fh.read()
    except OSError:
        return "host"
    if any(marker in cgroups for marker in ("docker", "containerd", "kubepods")):
        return "container"
    return "host"


# The runtime mode cannot change mid-process, so detect once at import.
RUNTIME_SCOPE = _detect_runtime_scope()


def data_disk_usage():
    """(usage, path) of the filesystem holding the app data; '/' fallback."""
    path = DATA_DIR if os.path.isdir(DATA_DIR) else "/"
    return psutil.disk_usage(path), path
