"""Server hardware inspection for the engineer monitoring page.

The runtime view IS the deployment view: on Linux production the container
shares the host kernel, so /proc + psutil describe the actual server (VM or
bare metal) — CPU model, RAM totals, everything. On Docker Desktop dev they
describe the desktop VM instead; host_metrics.RUNTIME_SCOPE (bug 2026-10-12)
lets the UI name which of the two the numbers belong to.

The inventory (CPU model, cores, GPUs, kernel, OS, hostname) cannot change
mid-process, so it is read once and cached; memory used/available moves, so
it is re-read per request. GPU detection is a best-effort probe chain — a
headless VM simply has no GPU, and that is reported honestly as an empty
list, never guessed at. All values are locale-free data; Indonesian
fallbacks ("Tidak terdeteksi" etc.) are the frontend's job.
"""
import os
import platform
import re
import shutil
import socket
import subprocess

import psutil

from services import host_metrics

# Vendor ids for the sysfs DRM fallback — includes the common virtual-GPU
# vendors so a VM with display passthrough still names itself.
_DRM_VENDORS = {
    "0x10de": "NVIDIA",
    "0x1002": "AMD",
    "0x8086": "Intel",
    "0x15ad": "VMware SVGA",
    "0x1af4": "virtio-gpu",
}


def _cpu_model() -> str | None:
    """'model name' from /proc/cpuinfo; ARM kernels name the fields differently."""
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.lower().startswith(("model name", "hardware", "bios model name")):
                    value = line.split(":", 1)[1].strip()
                    if value:
                        return value
    except OSError:
        pass
    return platform.processor() or None


def _os_pretty() -> str | None:
    """PRETTY_NAME from /etc/os-release — the runtime OS (container image on
    Docker deployments, which is honest: the container's userspace is what
    the app actually runs in)."""
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system() or None


def _detect_gpus() -> list[str]:
    """Probe chain, first hit wins: nvidia-smi (GPU passed through with the
    NVIDIA runtime) → lspci (rare inside slim images) → sysfs DRM vendor ids
    (name-less but proves passthrough). Empty list = nothing visible, the
    normal state for a headless server or VM."""
    gpus: list[str] = []

    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            out = subprocess.run(
                [smi, "--query-gpu=name,memory.total", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5)
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = [p.strip() for p in line.split(",", 1)]
                    gpus.append(parts[0] if len(parts) == 1 else f"{parts[0]} ({parts[1]})")
        except (OSError, subprocess.TimeoutExpired):
            pass

    if not gpus:
        lspci = shutil.which("lspci")
        if lspci:
            try:
                out = subprocess.run([lspci], capture_output=True, text=True, timeout=5)
                if out.returncode == 0:
                    for line in out.stdout.splitlines():
                        if re.search(r"vga|3d controller|display", line, re.I):
                            # "00:02.0 VGA compatible controller: Intel ..." → drop the slot id.
                            gpus.append(line.split(" ", 1)[1].strip() if " " in line else line)
            except (OSError, subprocess.TimeoutExpired):
                pass

    if not gpus:
        base = "/sys/class/drm"
        try:
            for card in sorted(os.listdir(base)):
                if not re.fullmatch(r"card\d+", card):
                    continue
                vpath = os.path.join(base, card, "device", "vendor")
                try:
                    with open(vpath, encoding="utf-8") as fh:
                        vid = fh.read().strip().lower()
                except OSError:
                    continue
                if vid in _DRM_VENDORS:
                    gpus.append(f"{_DRM_VENDORS[vid]} ({card}, vendor {vid})")
        except OSError:
            pass

    return gpus


_STATIC: dict | None = None


def _static_inventory() -> dict:
    """Read-once cache. Hardware inventory is fixed for the process lifetime;
    a benign first-call race (async workers) would only recompute identical
    data, so no lock is worth it."""
    global _STATIC
    if _STATIC is None:
        _STATIC = {
            "hostname": socket.gethostname(),
            "runtime_scope": host_metrics.RUNTIME_SCOPE,
            "os": _os_pretty(),
            "kernel": platform.release(),
            "cpu": {
                "model": _cpu_model(),
                "logical_cores": psutil.cpu_count(logical=True),
                "physical_cores": psutil.cpu_count(logical=False),
                "arch": platform.machine() or None,
            },
            "gpus": _detect_gpus(),
        }
    return _STATIC


def snapshot() -> dict:
    """Static inventory + the live memory split (total is fixed; used and
    available move, so they are re-read every call)."""
    inv = dict(_static_inventory())
    mem = psutil.virtual_memory()
    inv["memory"] = {"total": mem.total, "used": mem.used, "available": mem.available}
    return inv
