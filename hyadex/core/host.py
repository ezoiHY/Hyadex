"""Host memory pressure readings."""
from __future__ import annotations

from contextlib import suppress
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

from hyadex.core.config import (
    JOB_MAX_ACTIVE_RUNS,
    JOB_MIN_AVAILABLE_MEM_MB,
    MAX_ACTIVE_AGENT_RUNS,
    MIN_START_AVAILABLE_MEM_MB,
)
from hyadex.core.state import DARWIN_MEMORY_CACHE, DARWIN_MEMORY_CACHE_SECONDS


def parse_vm_stat_available_mb(stdout: str) -> int | None:
    """Derive available memory from ``vm_stat`` output.

    Free, inactive, speculative, and purgeable pages are all reclaimable
    without swapping, which is the closest macOS analogue of Linux
    ``MemAvailable``.
    """

    page_size = 4096
    pages: dict[str, int] = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Mach Virtual Memory Statistics"):
            match = re.search(r"page size of (\d+) bytes", stripped)
            if match:
                page_size = int(match.group(1))
            continue
        if ":" not in stripped:
            continue
        name, _, raw = stripped.partition(":")
        digits = raw.strip().rstrip(".")
        if digits.isdigit():
            pages[name.strip().lower()] = int(digits)
    if not pages:
        return None
    reclaimable = sum(
        pages.get(name, 0)
        for name in (
            "pages free",
            "pages inactive",
            "pages speculative",
            "pages purgeable",
        )
    )
    return int(reclaimable * page_size / (1024 * 1024))


def darwin_available_memory_mb() -> int | None:
    now = time.monotonic()
    if now - float(DARWIN_MEMORY_CACHE["at"]) < DARWIN_MEMORY_CACHE_SECONDS:
        return DARWIN_MEMORY_CACHE["mb"]
    value: int | None = None
    try:
        result = subprocess.run(
            ["vm_stat"],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0:
            value = parse_vm_stat_available_mb(result.stdout)
    except (OSError, subprocess.SubprocessError):
        value = None
    DARWIN_MEMORY_CACHE["at"] = now
    DARWIN_MEMORY_CACHE["mb"] = value
    return value


def host_pressure_snapshot() -> dict[str, Any]:
    load_1m: float | None = None
    load_per_cpu: float | None = None
    cpu_count = os.cpu_count() or 1
    with suppress(Exception):
        load_1m = float(os.getloadavg()[0])
        load_per_cpu = load_1m / max(cpu_count, 1)

    available_mem_mb: int | None = None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        with suppress(Exception):
            for line in meminfo.read_text(errors="replace").splitlines():
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        available_mem_mb = int(int(parts[1]) / 1024)
                    break
    elif sys.platform == "darwin":
        with suppress(Exception):
            available_mem_mb = darwin_available_memory_mb()

    return {
        "load_1m": load_1m,
        "load_per_cpu": load_per_cpu,
        "cpu_count": cpu_count,
        "available_mem_mb": available_mem_mb,
        "job_min_available_mem_mb": JOB_MIN_AVAILABLE_MEM_MB,
        "job_max_active_runs": JOB_MAX_ACTIVE_RUNS,
        "start_min_available_mem_mb": MIN_START_AVAILABLE_MEM_MB,
        "start_max_active_runs": MAX_ACTIVE_AGENT_RUNS,
    }
