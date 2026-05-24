"""nvidia-smi adapter — per-PID VRAM accounting + total GPU state.

Designed to be mockable: AIRLOCK_NVIDIA_SMI_PATH env var overrides the
binary so tests can swap in a stub.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class GpuSnapshot:
    available: bool
    total_mib: int = 0
    used_mib: int = 0
    free_mib: int = 0
    utilization_pct: int = 0
    error: str = ""


@dataclass
class GpuProcess:
    pid: int
    used_mib: int
    name: str = ""


def _nvidia_smi_path() -> str | None:
    override = os.environ.get("AIRLOCK_NVIDIA_SMI_PATH")
    if override:
        return override
    return shutil.which("nvidia-smi")


def gpu_snapshot() -> GpuSnapshot:
    path = _nvidia_smi_path()
    if path is None:
        return GpuSnapshot(available=False, error="nvidia-smi not found")
    try:
        out = subprocess.check_output(
            [path, "--query-gpu=memory.used,memory.free,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            timeout=3,
        ).decode().strip()
    except (subprocess.SubprocessError, OSError) as e:
        return GpuSnapshot(available=False, error=str(e))
    if not out:
        return GpuSnapshot(available=False, error="empty nvidia-smi output")
    line = out.splitlines()[0].strip()
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 4:
        return GpuSnapshot(available=False, error=f"unexpected output: {out!r}")
    try:
        return GpuSnapshot(
            available=True,
            used_mib=int(parts[0]),
            free_mib=int(parts[1]),
            total_mib=int(parts[2]),
            utilization_pct=int(parts[3]),
        )
    except ValueError as e:
        return GpuSnapshot(available=False, error=str(e))


def compute_apps() -> list[GpuProcess]:
    """Return all PIDs currently using GPU memory."""
    path = _nvidia_smi_path()
    if path is None:
        return []
    try:
        out = subprocess.check_output(
            [path, "--query-compute-apps=pid,used_memory,process_name",
             "--format=csv,noheader,nounits"],
            timeout=3,
        ).decode().strip()
    except (subprocess.SubprocessError, OSError):
        return []
    procs: list[GpuProcess] = []
    if not out:
        return procs
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
            mib = int(parts[1])
        except ValueError:
            continue
        name = parts[2] if len(parts) > 2 else ""
        procs.append(GpuProcess(pid=pid, used_mib=mib, name=name))
    return procs


def read_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            data = f.read()
        return data.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except (FileNotFoundError, PermissionError):
        return ""
