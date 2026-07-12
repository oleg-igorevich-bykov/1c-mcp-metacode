"""
Incremental loading package — stage 1 (metadata TXT/XML).

Public API:
- IncrementalLoadingState: SQLite wrapper for state lifecycle.
- compute_object_hash, compute_configuration_hash: deterministic hashes for diff.
- IncrementalReport: per-run report (added/changed/deleted counts, embedding_repass_needed_qns).
- MetadataIncrementalSync: orchestrates apply_added_object / apply_changed_object / apply_deleted_object.
- IncrementalLoadingScheduler: daemon-thread that runs one-shot + scheduled cycles.

Architectural docs: see plans/twinkly-purring-lake.md.
"""

from .state import IncrementalLoadingState, LockLease
from .hashing import compute_object_hash, compute_configuration_hash
from .report import IncrementalReport

__all__ = [
    "IncrementalLoadingState",
    "LockLease",
    "compute_object_hash",
    "compute_configuration_hash",
    "IncrementalReport",
]
