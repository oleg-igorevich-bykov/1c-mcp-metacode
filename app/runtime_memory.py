"""
Best-effort process/runtime memory diagnostics and trimming (stdlib-only).

Two responsibilities, both process-wide (not indexer-specific), which is why
this lives as a neutral leaf module next to ``runtime_metrics.py`` rather than
inside the ``indexer`` package: consumers in ``indexer`` and ``graphdb`` can
both import it without creating a cross-layer dependency.

1. Memory probes for full-load profiling. Two distinct measurement targets —
   do not mix them up when reading logs:

   - process metrics (``VmRSS`` / ``VmHWM`` from ``/proc/self/status``) cover
     ONLY the current process. BSL/XML worker processes are separate and
     invisible here. With a supported HWM reset this gives a per-stage peak of
     the orchestrator process (diagnostic metric).
   - cgroup metrics cover the whole container including worker processes. The
     cgroup peak is monotonic for the cgroup lifetime — NOT per-stage. This is
     the primary acceptance metric for the full-reload memory footprint.

2. ``trim_process_memory`` — explicit ``gc.collect() + malloc_trim(0)`` at
   heavy-phase boundaries to return allocator-retained (glibc arena) memory to
   the OS. Serialized + debounced so concurrent startup indexers do not fire
   overlapping or near-duplicate trims.

Every function degrades gracefully: on platforms without procfs/cgroupfs
(Windows dev machines) it returns ``None`` and never raises.
"""
from __future__ import annotations

import ctypes
import gc
import logging
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_PROC_STATUS = Path("/proc/self/status")
_CLEAR_REFS = Path("/proc/self/clear_refs")

# (current, peak) candidates: cgroup v2 first, then v1.
_CGROUP_CANDIDATES = (
    (
        Path("/sys/fs/cgroup/memory.current"),
        Path("/sys/fs/cgroup/memory.peak"),
    ),
    (
        Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
        Path("/sys/fs/cgroup/memory/memory.max_usage_in_bytes"),
    ),
)

# Cached availability so absent files are not re-probed on every stage.
_proc_status_available: Optional[bool] = None
_cgroup_paths: Optional[Tuple[Optional[Path], Optional[Path]]] = None
_reset_peak_supported: Optional[bool] = None

# Cached glibc malloc_trim binding (None = not resolved yet; False = unavailable).
_malloc_trim: Optional[object] = None

# Serialize + debounce trim_process_memory across threads (parallel startup
# indexers all live in the same process).
_trim_lock = threading.Lock()
_last_trim_monotonic: Optional[float] = None


def _read_status_field_kb(field: str) -> Optional[float]:
    global _proc_status_available
    if _proc_status_available is False:
        return None
    prefix = field + ":"
    try:
        with _PROC_STATUS.open("r", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith(prefix):
                    _proc_status_available = True
                    return float(line.split()[1])  # value is in kB
        _proc_status_available = True
        return None
    except (OSError, ValueError, IndexError):
        _proc_status_available = False
        return None


def read_rss_mb() -> Optional[float]:
    """Current RSS of this process (``VmRSS``), MB."""
    kb = _read_status_field_kb("VmRSS")
    return kb / 1024.0 if kb is not None else None


def read_peak_mb() -> Optional[float]:
    """RSS high-water mark of this process (``VmHWM``), MB.

    Per-stage only when :func:`reset_peak` is supported; otherwise this is the
    process-global peak.
    """
    kb = _read_status_field_kb("VmHWM")
    return kb / 1024.0 if kb is not None else None


def reset_peak() -> bool:
    """Best-effort reset of the process RSS high-water mark.

    Returns whether the reset is supported. Unsupported (non-Linux, or a
    container without write access to ``/proc/self/clear_refs``) is reported
    once per process; ``VmHWM`` readings then mean the global process peak.
    """
    global _reset_peak_supported
    if _reset_peak_supported is False:
        return False
    try:
        with _CLEAR_REFS.open("w") as f:
            f.write("5")
        _reset_peak_supported = True
        return True
    except OSError:
        if _reset_peak_supported is None:
            logger.info(
                "runtime_memory: VmHWM reset not supported — "
                "process_hwm_stage values are the process-global peak"
            )
        _reset_peak_supported = False
        return False


def _resolve_cgroup_paths() -> Tuple[Optional[Path], Optional[Path]]:
    global _cgroup_paths
    if _cgroup_paths is None:
        current_path: Optional[Path] = None
        peak_path: Optional[Path] = None
        try:
            for cand_current, cand_peak in _CGROUP_CANDIDATES:
                if cand_current.exists():
                    current_path = cand_current
                    peak_path = cand_peak if cand_peak.exists() else None
                    break
        except OSError:
            pass
        _cgroup_paths = (current_path, peak_path)
    return _cgroup_paths


def _read_bytes_file_mb(path: Path) -> Optional[float]:
    try:
        return int(path.read_text().strip()) / (1024.0 * 1024.0)
    except (OSError, ValueError):
        return None


def read_cgroup_mb() -> Tuple[Optional[float], Optional[float]]:
    """(current, peak) memory of the whole cgroup (container), MB.

    Covers all processes in the container, including BSL/XML workers. The peak
    is cumulative over the cgroup lifetime.
    """
    current_path, peak_path = _resolve_cgroup_paths()
    current = _read_bytes_file_mb(current_path) if current_path is not None else None
    peak = _read_bytes_file_mb(peak_path) if peak_path is not None else None
    return current, peak


def format_mem_snapshot() -> Optional[str]:
    """One-line snapshot of all available metrics, or None when none apply."""
    parts = []
    rss = read_rss_mb()
    if rss is not None:
        parts.append(f"process_rss={rss:.1f}MB")
    hwm = read_peak_mb()
    if hwm is not None:
        parts.append(f"process_hwm_stage={hwm:.1f}MB")
    cg_current, cg_peak = read_cgroup_mb()
    if cg_current is not None:
        parts.append(f"cgroup_current={cg_current:.1f}MB")
    if cg_peak is not None:
        parts.append(f"cgroup_peak_global={cg_peak:.1f}MB")
    return " ".join(parts) if parts else None


def format_run_summary() -> Optional[str]:
    """Whole-run summary: process-global peak (ru_maxrss) + cgroup peak."""
    parts = []
    try:
        import resource  # not available on Windows

        # On Linux ru_maxrss is in kB.
        ru_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        parts.append(f"process_peak_total={ru_kb / 1024.0:.1f}MB")
    except Exception:
        pass
    _, cg_peak = read_cgroup_mb()
    if cg_peak is not None:
        parts.append(f"cgroup_peak_global={cg_peak:.1f}MB")
    return " ".join(parts) if parts else None


def _resolve_malloc_trim() -> Optional[object]:
    """Return a cached ``libc.malloc_trim`` callable, or None on non-glibc.

    Cached at module level so ``CDLL("libc.so.6")`` runs at most once. A
    non-glibc platform (Windows dev machine, musl) caches ``False`` and never
    retries.
    """
    global _malloc_trim
    if _malloc_trim is None:
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=False)
            fn = libc.malloc_trim
            fn.argtypes = [ctypes.c_size_t]
            fn.restype = ctypes.c_int
            _malloc_trim = fn
        except (OSError, AttributeError):
            _malloc_trim = False
    return _malloc_trim if _malloc_trim not in (None, False) else None


def trim_process_memory(
    reason: str = "",
    *,
    enabled: bool = True,
    min_interval_seconds: float = 30.0,
    force: bool = False,
) -> dict:
    """Best-effort return of allocator-retained memory to the OS.

    Runs ``gc.collect()`` then glibc ``malloc_trim(0)``. Intended ONLY for
    heavy-phase boundaries (full metadata load done, BSL Phase A done, Phase B
    finalize done) — never inside hot loops, because both calls are
    process-wide stop-the-world.

    Serialized and debounced: parallel startup indexers (vector / bsl_code /
    object_summary) share one process, so a module-level lock guards an atomic
    ``min_interval_seconds`` check. A trim within the window (e.g. the Phase A
    and Phase B-finalize boundaries landing seconds apart when Phase B is
    skipped/degraded) is skipped. ``force=True`` bypasses the debounce.

    Never raises: any failure degrades to a no-op with ``error`` in the result.
    On Windows/non-glibc ``malloc_trim`` is None.
    """
    if not enabled:
        return {"enabled": False}

    result: dict = {"enabled": True, "reason": reason}
    try:
        with _trim_lock:
            global _last_trim_monotonic
            now = time.monotonic()
            if (
                not force
                and _last_trim_monotonic is not None
                and (now - _last_trim_monotonic) < min_interval_seconds
            ):
                ago = now - _last_trim_monotonic
                result["skipped"] = "debounced"
                logger.info(
                    "[MEM-TRIM] %s: skipped=debounced (last trim %.1fs ago)",
                    reason, ago,
                )
                return result

            before = format_mem_snapshot()
            collected = gc.collect()
            result["gc_collected"] = collected

            trim_fn = _resolve_malloc_trim()
            if trim_fn is None:
                result["malloc_trim"] = None
            else:
                try:
                    result["malloc_trim"] = int(trim_fn(0))
                except Exception as e:  # pragma: no cover - defensive
                    result["malloc_trim"] = None
                    result["error"] = repr(e)

            after = format_mem_snapshot()
            result["before"] = before
            result["after"] = after
            _last_trim_monotonic = time.monotonic()

            logger.info(
                "[MEM-TRIM] %s: gc_collected=%s malloc_trim=%s before=[%s] after=[%s]",
                reason,
                result.get("gc_collected"),
                result.get("malloc_trim"),
                before or "n/a",
                after or "n/a",
            )
    except Exception as e:  # pragma: no cover - must never propagate
        result.setdefault("error", repr(e))
        try:
            logger.warning("[MEM-TRIM] %s: failed (%r)", reason, e)
        except Exception:
            pass
    return result
