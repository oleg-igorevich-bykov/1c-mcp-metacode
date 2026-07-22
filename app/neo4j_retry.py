"""
Shared classification/retry helpers for transient Neo4j errors.

Neo4j classifies lock contention and short-lived overload conditions as
TransientError specifically so the client retries. The driver's own managed
transactions (session.execute_write/execute_read) retry a subset of these
automatically, but that does not help call sites that catch a broad
`except Exception` around the whole write and give up immediately (see
bsl_worker.py batch writes, indexer/orchestrator.py top-level pipeline).

`LockClientStopped` and `TransactionTimedOutClientConfiguration` are
technically ClientErrors, not TransientErrors, but under the fleet's shared
Neo4j instance they are the direct, reproducible consequence of the same
lock contention (observed 22.07 on a full 18-container concurrent
provisioning run) and should be treated the same way for retry purposes.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

from neo4j.exceptions import Neo4jError

logger = logging.getLogger(__name__)

T = TypeVar("T")

_TRANSIENT_MARKERS = (
    "TransientError",
    "DeadlockDetected",
    "LockClientStopped",
    "TransactionTimedOutClientConfiguration",
)


def is_transient_neo4j_error(exc: BaseException) -> bool:
    """True if `exc` looks like Neo4j lock contention / short-lived overload
    (worth retrying), rather than a genuine data or query problem."""
    if not isinstance(exc, Neo4jError):
        return False
    code = getattr(exc, "code", "") or ""
    text = str(exc)
    return any(marker in code or marker in text for marker in _TRANSIENT_MARKERS)


def call_with_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = 4,
    base_delay: float = 0.5,
    what: str = "Neo4j operation",
) -> T:
    """Call `fn()`, retrying with linear backoff while the raised error
    classifies as transient. Non-transient errors and exhausted attempts
    are re-raised as-is."""
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Neo4jError as e:
            if is_transient_neo4j_error(e) and attempt < max_attempts:
                logger.warning(
                    "%s: transient Neo4j error on attempt %d/%d, retrying: %s",
                    what, attempt, max_attempts, e,
                )
                time.sleep(base_delay * attempt)  # linear backoff
                continue
            raise
