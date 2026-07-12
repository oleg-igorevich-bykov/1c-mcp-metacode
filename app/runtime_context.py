"""Per-process runtime context.

Currently exposes a single value: a `run_id` generated once per process and
reused by aggregated usage tracking (`app/runtime_metrics.py`) and the
object_summary pipeline.

The id format `YYYYMMDD-HHMMSS-<6hex>` is human-sortable, short enough for
logs, and gives a hint about when the run started without needing to open
SQLite.
"""

from __future__ import annotations

import logging
import secrets
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_RUN_ID: Optional[str] = None
_LOCK = threading.Lock()


def get_run_id() -> str:
    global _RUN_ID
    if _RUN_ID is not None:
        return _RUN_ID
    with _LOCK:
        if _RUN_ID is not None:
            return _RUN_ID
        now = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        _RUN_ID = f"{now}-{secrets.token_hex(3)}"
        logger.info("Runtime run_id=%s", _RUN_ID)
    return _RUN_ID
