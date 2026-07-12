"""
In-memory stats cache for the web console.
Populated at bootstrap and refreshed by lifecycle hooks (startup indexers
complete, scheduled incremental cycles that changed the graph) and by an
admin-only manual refresh. Reads via `get_stats_cache()` stay fast; the heavy
`loader.get_statistics()` runs only inside `refresh_console_stats_cache()`.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_cache: Optional[dict] = None
_lock = threading.Lock()


class StatsRefreshError(RuntimeError):
    """Raised by `refresh_console_stats_cache(raise_on_error=True)` so the manual
    refresh endpoint can map failures to an HTTP 503 with a stable error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def get_stats_cache() -> Optional[dict]:
    return _cache


def refresh_console_stats_cache(
    source: str = "unknown",
    block: bool = True,
    raise_on_error: bool = False,
) -> Optional[dict]:
    """Recompute the console stats cache from the Neo4j loader.

    All correctness-critical callers (lifecycle hooks, manual refresh) use
    ``block=True``: refreshes serialize on ``_lock`` so a needed refresh is
    never skipped and two heavy ``get_statistics()`` never run concurrently.
    ``block=False`` is an opportunistic path: if a refresh is already running it
    returns the current cache without recomputing.

    ``raise_on_error=True`` (manual endpoint) surfaces failures as
    :class:`StatsRefreshError`; otherwise (lifecycle) errors are logged and the
    previous cache is preserved.
    """
    global _cache

    if not _lock.acquire(blocking=block):
        logger.debug("Console stats refresh already in progress (source=%s), skipping", source)
        return _cache
    try:
        from config import settings
        from mcpsrv.neo4j_init import get_loader

        loader = get_loader()
        if loader is None:
            if raise_on_error:
                raise StatsRefreshError("stats_not_ready", "Neo4j loader not available")
            logger.warning("Console stats cache: Neo4j loader not available, skipping")
            return _cache
        try:
            started = time.monotonic()
            stats = loader.get_statistics(settings.project_name)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            _cache = {
                "project_name": settings.project_name,
                "stats": stats,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "refresh_source": source,
                "refresh_duration_ms": round(elapsed_ms),
            }
            logger.info(
                "Console stats cache refreshed (%d keys, source=%s, %dms)",
                len(stats), source, round(elapsed_ms),
            )
            return _cache
        except StatsRefreshError:
            raise
        except Exception as e:
            if raise_on_error:
                raise StatsRefreshError("stats_refresh_failed", str(e))
            logger.error("Console stats cache refresh failed (source=%s): %s", source, e)
            return _cache
    finally:
        _lock.release()
