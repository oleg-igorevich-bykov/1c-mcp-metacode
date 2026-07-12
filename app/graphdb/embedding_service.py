"""
Embedding service for generating vector embeddings via OpenAI-compatible API.
"""
from __future__ import annotations

import base64
import logging
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from config import settings
from graphdb.embedding_chunks import split_text_for_embedding, weighted_mean_pool
from graphdb.embedding_text_format import (
    EmbeddingFormatSpec,
    build_embedding_format_spec,
    detect_embedding_text_profile,
    detect_embedding_transport,
)


UsageDict = Optional[Dict[str, Any]]


class EmbeddingUnavailableError(RuntimeError):
    """Raised by our own preflight paths when embedding is known-unavailable.

    Distinct from the raw OpenAI/httpx exceptions that surface from actual API
    calls: those are classified via ``is_embedding_unavailable_error``. This
    typed error is what preflight code (service is None / probe failed) raises so
    callers can catch a single quiet-degradation branch without traceback.
    """


@dataclass
class EmbeddingAvailability:
    """Startup embedding availability contract shared by all indexer starters.

    ``enabled`` — at least one embedding feature is on in settings.
    ``available`` — a bounded probe reached the endpoint this pass.
    ``dimension`` — vector length from the probe (only when available).
    ``reason`` — short, traceback-free explanation when not available.
    """
    enabled: bool
    available: bool
    dimension: Optional[int] = None
    reason: str = ""


@dataclass
class EmbeddingBatchResult:
    embeddings: List[Optional[List[float]]]
    input_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_amount: Optional[float] = None
    cost_unit: Optional[str] = None
    cost_source: str = "unknown"
    api_calls: int = 0


def _merge_usage(a: UsageDict, b: UsageDict) -> UsageDict:
    """Sum two optional usage dicts. None acts as a neutral element.

    Keys merged: prompt_tokens, total_tokens, cost. Cost-bearing side wins
    cost_unit. Missing numeric keys are treated as 0 when at least one side
    has them; otherwise the field stays absent.
    """
    if a is None and b is None:
        return None
    if a is None:
        return dict(b) if b is not None else None
    if b is None:
        return dict(a)
    out: Dict[str, Any] = {}
    for key in ("prompt_tokens", "total_tokens"):
        va = a.get(key)
        vb = b.get(key)
        if va is None and vb is None:
            continue
        out[key] = int(va or 0) + int(vb or 0)
    ca = a.get("cost")
    cb = b.get("cost")
    if ca is not None or cb is not None:
        out["cost"] = float(ca or 0.0) + float(cb or 0.0)
        out["cost_unit"] = a.get("cost_unit") if ca is not None else b.get("cost_unit")
    return out


def _extract_openai_usage(response: Any) -> UsageDict:
    """Pull token + cost fields out of an OpenAI-compatible embeddings response."""
    raw = getattr(response, "usage", None)
    if raw is None and isinstance(response, dict):
        raw = response.get("usage")
    if raw is None:
        return None
    out: Dict[str, Any] = {}
    for key in ("prompt_tokens", "total_tokens"):
        val = getattr(raw, key, None)
        if val is None and isinstance(raw, dict):
            val = raw.get(key)
        if isinstance(val, (int, float)):
            out[key] = int(val)
    cost = getattr(raw, "cost", None)
    if cost is None and isinstance(raw, dict):
        cost = raw.get("cost")
    if isinstance(cost, (int, float)):
        out["cost"] = float(cost)
        out["cost_unit"] = "credits"
    return out or None


def _build_batch_result(
    embeddings: List[Optional[List[float]]], usage: UsageDict, *, api_calls: int = 0,
) -> "EmbeddingBatchResult":
    if usage is None:
        return EmbeddingBatchResult(embeddings=embeddings, api_calls=api_calls)
    cost = usage.get("cost")
    cost_source = "provider_reported" if cost is not None else "unknown"
    return EmbeddingBatchResult(
        embeddings=embeddings,
        input_tokens=usage.get("prompt_tokens"),
        total_tokens=usage.get("total_tokens"),
        cost_amount=float(cost) if cost is not None else None,
        cost_unit=usage.get("cost_unit") if cost is not None else None,
        cost_source=cost_source,
        api_calls=api_calls,
    )


def _usage_from_batch_result(result: "EmbeddingBatchResult") -> UsageDict:
    out: Dict[str, Any] = {}
    if result.input_tokens is not None:
        out["prompt_tokens"] = result.input_tokens
    if result.total_tokens is not None:
        out["total_tokens"] = result.total_tokens
    if result.cost_amount is not None:
        out["cost"] = result.cost_amount
        out["cost_unit"] = result.cost_unit
    return out or None


_INPUT_TOO_LONG_MARKERS = (
    "input too long",
    "input is too long",
    "too many tokens",
    "maximum context",
    "context length",
    "max input tokens",
    "token limit",
    "maximum sequence length",
    "exceeds",
)
_INPUT_TOO_LONG_STATUSES = (400, 413, 422)
_ADAPTIVE_MAX_DEPTH = 4
_ADAPTIVE_MIN_CHARS = 500
_ADAPTIVE_OVERLAP = 0
_ADAPTIVE_MAX_SUBCHUNKS = 16

# Transient/external-unavailability signatures. Shared by the retry heuristic
# (EmbeddingService._is_transient_error) and the module-level
# is_embedding_unavailable_error classifier so both stay in sync.
_TRANSIENT_STATUSES = (408, 409, 425, 429, 499)
_TRANSIENT_MARKERS = (
    "timeout", "timed out", "read timed out", "connect timeout",
    "too many requests", "rate limit", "temporarily unavailable",
    "connection reset", "connection aborted", "connection refused",
    "remote disconnected", "server error", "bad gateway",
    "service unavailable", "gateway timeout", "econnreset", "etimedout",
    # httpcore/httpx phrasing for a dropped keep-alive connection (the concrete
    # log case: openai.APIConnectionError caused by httpcore.RemoteProtocolError).
    # Deliberately httpcore-specific so Neo4j bolt / SQLite messages don't match.
    "server disconnected", "disconnected without sending a response",
    # Provider returned HTTP 200 with empty/missing `data`; the OpenAI SDK's
    # own response parser raises ValueError("No embedding data received").
    # Treated as an external endpoint degradation (retry, then quiet outage),
    # not a code bug — carries no HTTP status, so only marker-matching classifies it.
    "no embedding data received",
)

# Exception *class-name* markers (substring match on the lowercased type name).
# Deliberately narrow: only the openai/httpx types that the embedding HTTP client
# raises in this codebase. Generic names (ConnectionError/ReadError/WriteError/
# ConnectError/TimeoutException/PoolTimeout) are intentionally excluded — the
# classifier is applied to whole Phase B worker exceptions, whose Neo4j vector
# write + SQLite done-mark run after the embedding call; matching those generic
# names would misclassify an infra write failure (e.g. builtin ConnectionError)
# as an embedding outage and silence its traceback.
_OUTAGE_EXC_NAME_MARKERS = (
    "apiconnectionerror", "apitimeouterror", "remoteprotocolerror",
)

logger = logging.getLogger(__name__)


def _extract_http_status(err: Exception) -> Optional[int]:
    """Extract an HTTP status code from heterogeneous exception shapes.

    OpenAI SDK exposes status on the exception itself; httpx.HTTPStatusError
    keeps it on err.response.status_code.
    """
    for attr in ("status_code", "status", "http_status", "code"):
        if hasattr(err, attr):
            try:
                return int(getattr(err, attr))
            except Exception:
                pass
    response = getattr(err, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            if hasattr(response, attr):
                try:
                    return int(getattr(response, attr))
                except Exception:
                    pass
    return None


def _error_message(err: Exception) -> str:
    """Combine str(err) with the response body (httpx) for substring matching."""
    msg = str(err) or ""
    response = getattr(err, "response", None)
    if response is not None:
        try:
            body = response.text
            if body:
                msg = f"{msg} {body}"
        except Exception:
            pass
    return msg.lower()


def _is_input_too_long(err: Exception) -> bool:
    """Detect 'input too long' errors: HTTP 400/413/422 AND a known phrase.

    Requires BOTH so rate-limit/transient errors are not misclassified. Not
    treated as endpoint-unavailable: the adaptive split handles these.
    """
    try:
        status = _extract_http_status(err)
        if not isinstance(status, int) or status not in _INPUT_TOO_LONG_STATUSES:
            return False
        msg = _error_message(err)
        return any(m in msg for m in _INPUT_TOO_LONG_MARKERS)
    except Exception:
        return False


def _is_no_embedding_data_error(err: Exception) -> bool:
    """Match ONLY the SDK's ValueError("No embedding data received") (provider 200 with
    empty/missing `data`). Narrow on purpose so the encoding_format=float fallback does not
    fire on timeouts, 429/5xx, auth/billing or input-too-long — those go through normal retry.
    """
    return "no embedding data received" in _error_message(err)


def _iter_exception_chain(err: BaseException, max_depth: int = 5):
    """Yield ``err`` then its ``__cause__`` then its ``__context__``, without cycles.

    Breadth-first with ``__cause__`` enqueued before ``__context__`` so the direct
    cause (``raise ... from ...``) is visited before the incidental context — this
    matters for ``format_embedding_error``, which reports the first link after
    ``err`` as ``caused by``. Bounded by ``max_depth`` and de-duplicated by
    ``id()`` so a self-referential or deeply nested chain can never loop. Needed
    because the real outage signal (e.g. httpcore.RemoteProtocolError) is often
    only in ``__cause__`` of the surfaced exception (e.g. openai.APIConnectionError).
    """
    seen = set()
    queue = deque([(err, 0)])
    while queue:
        cur, depth = queue.popleft()
        if cur is None or id(cur) in seen or depth > max_depth:
            continue
        seen.add(id(cur))
        yield cur
        queue.append((getattr(cur, "__cause__", None), depth + 1))
        queue.append((getattr(cur, "__context__", None), depth + 1))


def _matches_outage_signatures(err: Exception) -> bool:
    """Chain-aware match of external-outage signatures (status/markers/class name).

    Walks the exception chain and returns True if any link carries a transient
    HTTP status, a transient text marker, or an embedding-client-specific
    exception class name. Shared by ``is_embedding_unavailable_error`` and
    ``EmbeddingService._is_transient_error`` so retry and quiet-degradation stay
    in sync. Does NOT apply the input-too-long guard — callers layer that on.
    """
    try:
        for link in _iter_exception_chain(err):
            status = _extract_http_status(link)
            if isinstance(status, int) and (status in _TRANSIENT_STATUSES or 500 <= status <= 599):
                return True
            cls_name = type(link).__name__.lower()
            if any(m in cls_name for m in _OUTAGE_EXC_NAME_MARKERS):
                return True
            msg = _error_message(link)
            if any(m in msg for m in _TRANSIENT_MARKERS):
                return True
        return False
    except Exception:
        return False


def is_embedding_unavailable_error(err: Exception) -> bool:
    """Classify an exception as an expected external embedding-endpoint outage.

    True for connection refused / timeout / 408/409/425/429/499 / 5xx, the
    httpcore/openai connection-drop chain (APIConnectionError -> RemoteProtocolError)
    and the typed EmbeddingUnavailableError. Explicitly excludes input-too-long,
    which is a payload problem handled by the adaptive split, not an outage.
    """
    if isinstance(err, EmbeddingUnavailableError):
        return True
    try:
        if _is_input_too_long(err):
            return False
        return _matches_outage_signatures(err)
    except Exception:
        return False


def _format_error_link(err: BaseException) -> str:
    """Single-line ``Name (HTTP n): first message line`` for one exception link."""
    status = _extract_http_status(err)
    raw = (str(err) or "").strip()
    first_line = raw.splitlines()[0] if raw else ""
    name = type(err).__name__
    head = f"{name} (HTTP {status})" if status is not None else name
    return f"{head}: {first_line}" if first_line else head


def format_embedding_error(err: Exception) -> str:
    """Short, single-line, traceback-free reason for a known embedding failure.

    Unrolls up to one cause/context link so a wrapped connection drop reads as
    ``APIConnectionError: Connection error.; caused by RemoteProtocolError:
    Server disconnected...`` instead of hiding the real reason inside __cause__.
    """
    links = list(_iter_exception_chain(err, max_depth=2))
    if not links:
        return type(err).__name__
    head = _format_error_link(links[0])
    if len(links) > 1:
        return f"{head}; caused by {_format_error_link(links[1])}"
    return head


def any_embedding_feature_enabled() -> bool:
    """True if at least one embedding feature is enabled in settings.

    Single source of truth for the enablement gate used by the shared service
    getter, the dimension probe and the startup availability probe. The BSL
    vector gate requires both the BSL master flag and the embedding sub-flag.
    """
    bsl_code_embedding_enabled = bool(
        getattr(settings, "enable_bsl_code_search", False)
        and getattr(settings, "enable_bsl_code_embedding", False)
    )
    return bool(
        settings.enable_routine_description_embedding
        or settings.enable_metadata_description_embedding
        or bsl_code_embedding_enabled
        or getattr(settings, "object_summary_enabled", False)
    )


def probe_embedding_availability() -> EmbeddingAvailability:
    """Bounded startup probe of embedding availability without touching the singleton.

    Builds a transient EmbeddingService with a short timeout, a single attempt
    and model-info detection disabled, then issues one small fingerprint embed.
    Never raises: a dead/slow endpoint returns available=False with a short
    reason. When enabled features are all off, no service is constructed.
    """
    if not any_embedding_feature_enabled():
        return EmbeddingAvailability(
            enabled=False, available=False, dimension=None,
            reason="embedding features disabled",
        )
    probe_timeout = float(getattr(settings, "embedding_startup_probe_timeout_seconds", 10.0) or 10.0)
    try:
        service = EmbeddingService(
            timeout_seconds=probe_timeout, max_retries=1, detect_context_via_api=False,
        )
        vec = service.embed_for_fingerprint("test")
    except Exception as e:
        return EmbeddingAvailability(
            enabled=True, available=False, dimension=None,
            reason=format_embedding_error(e),
        )
    if not vec:
        return EmbeddingAvailability(
            enabled=True, available=False, dimension=None,
            reason="probe returned empty vector",
        )
    return EmbeddingAvailability(
        enabled=True, available=True, dimension=len(vec), reason="",
    )

# Singleton instance
_embedding_service_instance: Optional['EmbeddingService'] = None
_embedding_service_checked: bool = False
_embedding_service_lock = threading.Lock()


def get_embedding_service() -> Optional['EmbeddingService']:
    """
    Get or create singleton instance of EmbeddingService.
    This ensures model info is fetched only once at startup.

    Returns None if embeddings are disabled in settings.

    Returns:
        Singleton EmbeddingService instance or None if embeddings disabled
    """
    global _embedding_service_instance, _embedding_service_checked

    if _embedding_service_checked:
        return _embedding_service_instance

    with _embedding_service_lock:
        if _embedding_service_checked:
            return _embedding_service_instance

        # Check if embeddings are enabled. The shared EmbeddingService is used
        # by description embeddings, BSL code embeddings and object summary.
        if not any_embedding_feature_enabled():
            logger.info("Embeddings disabled, EmbeddingService will not be initialized")
            _embedding_service_instance = None
            _embedding_service_checked = True
            return None

        # Create singleton instance. Mark the check complete only after the
        # constructor succeeds; otherwise a transient init failure would poison
        # the singleton as checked-but-None for the whole process.
        if _embedding_service_instance is None:
            _embedding_service_instance = EmbeddingService()
        _embedding_service_checked = True
        return _embedding_service_instance


def reset_embedding_service_singleton() -> None:
    """Test hook: clear the cached embedding service and checked flag."""
    global _embedding_service_instance, _embedding_service_checked
    with _embedding_service_lock:
        _embedding_service_instance = None
        _embedding_service_checked = False


class EmbeddingService:
    """Service for generating embeddings via OpenAI-compatible API"""

    def __init__(
        self,
        *,
        timeout_seconds: Optional[float] = None,
        max_retries: Optional[int] = None,
        detect_context_via_api: Optional[bool] = None,
    ):
        """Initialize embedding service with configured API settings.

        The keyword overrides let a caller build a short-lived probe instance
        (e.g. the startup vector-index dimension probe) with a bounded timeout,
        a single attempt and model-info detection disabled, without mutating the
        shared singleton. When an override is None the settings-derived default
        is used, so the normal singleton path is unchanged.
        """
        self.api_base = settings.embedding_api_base
        self.api_key = settings.embedding_api_key
        self.model = settings.embedding_model
        self.batch_size = settings.embedding_batch_size
        # Minimal robustness controls (configurable via settings; defaults used if absent)
        self.timeout_seconds: float = (
            float(timeout_seconds) if timeout_seconds is not None
            else float(getattr(settings, "embedding_timeout_seconds", 30.0) or 30.0)
        )
        self.max_retries: int = (
            int(max_retries) if max_retries is not None
            else int(getattr(settings, "embedding_max_retries", 3) or 3)
        )
        # Backoff/jitter for retries
        self.backoff_base: float = float(getattr(settings, "embedding_retry_backoff_base_seconds", 0.5) or 0.5)
        self.backoff_cap: float = float(getattr(settings, "embedding_retry_backoff_max_seconds", 4.0) or 4.0)
        self.backoff_jitter: float = float(getattr(settings, "embedding_retry_jitter_seconds", 0.2) or 0.2)
        # Rescue-path: on "No embedding data received" retry the same OpenAI-compatible
        # batch once with encoding_format="float" inside the same attempt (no extra retry).
        self.float_fallback_on_no_data: bool = bool(
            getattr(settings, "embedding_float_fallback_on_no_data", True)
        )
        # Separate timeout for model info retrieval (fallback to main timeout when None)
        _mi_to = getattr(settings, "embedding_model_info_timeout_seconds", None)
        self.model_info_timeout_seconds: float = float(_mi_to) if _mi_to is not None else self.timeout_seconds

        if not self.api_base or not self.model:
            raise ValueError("Embedding API configuration is incomplete (api_base and model required)")

        # Validate API base URL
        if not self.api_base.startswith(('http://', 'https://')):
            raise ValueError(
                f"Invalid EMBEDDING_API_BASE: '{self.api_base}'. "
                f"URL must start with 'http://' or 'https://'. "
                f"Example: https://api.openai.com/v1"
            )

        # Resolve effective text-format profile and transport.
        cfg_profile = (getattr(settings, "embedding_text_format_profile", "auto") or "auto").strip().lower()
        cfg_transport = (getattr(settings, "embedding_transport", "auto") or "auto").strip().lower()
        self.text_format_profile: str = (
            detect_embedding_text_profile(self.model) if cfg_profile == "auto" else cfg_profile
        )
        self.transport: str = (
            detect_embedding_transport(self.api_base) if cfg_transport == "auto" else cfg_transport
        )
        if self.transport == "gemini_native" and self.text_format_profile != "gemini":
            raise ValueError(
                f"EMBEDDING_TRANSPORT=gemini_native requires a gemini text-format profile, "
                f"got profile={self.text_format_profile!r} for model={self.model!r}"
            )
        self._description_query_instruction: str = getattr(
            settings, "embedding_description_query_instruction", ""
        ) or ""
        # Identity spec for raw fingerprint path.
        self._fingerprint_spec: EmbeddingFormatSpec = build_embedding_format_spec(
            profile="none",
            transport=self.transport,
            side="document",
            purpose="description",
            description_instruction="",
        )

        try:
            import openai
            _proxy = settings.embedding_proxy or None
            _http_client = openai.DefaultHttpxClient(proxy=_proxy) if _proxy else None
            # max_retries=0: retry policy lives only in our _embed_with_retries();
            # the SDK's own retries would add duplicate attempts and noisy logs.
            self.client = openai.OpenAI(
                api_key=self.api_key or "",
                base_url=self.api_base,
                max_retries=0,
                **({"http_client": _http_client} if _http_client else {})
            )
        except ImportError as e:
            raise ImportError(f"openai package not installed or missing dependency: {e}. Install with: pip install openai")

        # Lazy httpx client for the native Gemini REST transport (shares proxy with OpenAI path).
        self._gemini_http_client = None
        self._gemini_proxy = _proxy

        # Detect model max input tokens via API (if supported) and compute safe chunk size in characters
        _detect_context = (
            bool(detect_context_via_api) if detect_context_via_api is not None
            else bool(getattr(settings, "embedding_detect_context_via_api", True))
        )
        self.max_input_tokens: Optional[int] = None
        if _detect_context:
            self.max_input_tokens = self._detect_max_input_tokens()

        # Fallback to configured value when API does not expose limits
        fallback_tokens = int(getattr(settings, "embedding_max_input_tokens_fallback", 8192) or 8192)
        safety_ratio = float(getattr(settings, "embedding_chunk_safety_ratio", 0.9) or 0.9)
        chars_per_token = float(getattr(settings, "embedding_chars_per_token_fallback", 2.0) or 2.0)

        max_tokens = int(self.max_input_tokens) if (self.max_input_tokens and self.max_input_tokens > 0) else fallback_tokens
        safe_tokens = max(512, int(max_tokens * safety_ratio))
        # Convert tokens to characters conservatively (no local tokenizer present)
        self.effective_chunk_chars: int = max(1000, int(safe_tokens * chars_per_token))

        _proxy_display = None
        if _proxy:
            try:
                from urllib.parse import urlparse
                _p = urlparse(_proxy)
                _proxy_display = (
                    f"{_p.scheme}://{_p.hostname}:{_p.port}"
                    if _p.port else f"{_p.scheme}://{_p.hostname}"
                )
            except Exception:
                _proxy_display = "<configured>"
        logger.info(
            f"EmbeddingService initialized: model={self.model}, api_base={self.api_base}, "
            f"proxy={_proxy_display or 'none'}, "
            f"profile={self.text_format_profile}, transport={self.transport}, "
            f"max_input_tokens={self.max_input_tokens or 'n/a'} (fallback={fallback_tokens}), "
            f"effective_chunk_chars={self.effective_chunk_chars}"
        )

    def _detect_max_input_tokens(self) -> Optional[int]:
        """
        Try to retrieve model's max input tokens from OpenAI-compatible /models API.
        Returns integer tokens on success, or None if unavailable.
        """
        # Retry models.retrieve() with the same timeout policy
        for attempt in range(1, max(1, self.max_retries) + 1):
            try:
                info = self.client.models.retrieve(self.model, timeout=self.model_info_timeout_seconds)
                break
            except Exception as e:
                if attempt >= max(1, self.max_retries):
                    logger.warning("Failed to retrieve model info for context length detection: %s", e)
                    return None
                if not self._is_transient_error(e):
                    logger.warning("Model info retrieval failed (non-transient): %s", e)
                    return None
                self._sleep_backoff(attempt)

        # Try attribute-style access first, then dict-like payloads
        candidates = ("context_length", "max_input_tokens", "max_tokens", "max_model_len", "max_context_tokens")

        def _as_dict(obj: Any) -> Dict[str, Any]:
            try:
                if isinstance(obj, dict):
                    return obj
                if hasattr(obj, "model_dump"):
                    return obj.model_dump()  # pydantic v2
                if hasattr(obj, "dict"):
                    return obj.dict()       # pydantic v1
                if hasattr(obj, "to_dict"):
                    return obj.to_dict()
            except Exception:
                pass
            # Best effort: reflect public attributes
            d: Dict[str, Any] = {}
            for k in dir(obj):
                if k.startswith("_"):
                    continue
                try:
                    v = getattr(obj, k)
                    if isinstance(v, (int, float, str, dict, list)):
                        d[k] = v
                except Exception:
                    pass
            return d

        # 1) Direct attributes
        for key in candidates:
            try:
                val = getattr(info, key, None)
                if isinstance(val, (int, float)) and int(val) > 0:
                    return int(val)
            except Exception:
                pass

        # 2) Dict-like payload and common nested containers
        data = _as_dict(info)
        # Shallow
        for key in candidates:
            val = data.get(key)
            if isinstance(val, (int, float)) and int(val) > 0:
                return int(val)
        # Nested common containers
        for container in ("usage", "limits", "metadata", "spec", "config", "capabilities"):
            sub = data.get(container)
            if isinstance(sub, dict):
                for key in candidates:
                    val = sub.get(key)
                    if isinstance(val, (int, float)) and int(val) > 0:
                        return int(val)

        logger.warning("Model info did not expose context length for '%s'", self.model)
        return None

    def get_embedding(self, text: str, *, format_spec: EmbeddingFormatSpec) -> Optional[List[float]]:
        """
        Get embedding for a single text using the provided format spec.

        Returns None for empty/whitespace/None input.
        """
        if text is None or text.strip() == "":
            return None
        results = self.get_embeddings([text], format_spec=format_spec)
        return results[0] if results else None

    def embed_for_fingerprint(self, text: str) -> Optional[List[float]]:
        """
        Raw embedding helper for project-fingerprint and vector-dimension probe.
        This is the only public path that bypasses an explicit EmbeddingFormatSpec.
        """
        return self.get_embedding(text, format_spec=self._fingerprint_spec)

    def get_embeddings(self, texts: List[str], *, format_spec: EmbeddingFormatSpec) -> List[Optional[List[float]]]:
        """Backwards-compatible vectors-only wrapper around ``get_embeddings_with_usage``."""
        return self.get_embeddings_with_usage(texts, format_spec=format_spec).embeddings

    def get_embeddings_with_usage(
        self, texts: List[str], *, format_spec: EmbeddingFormatSpec
    ) -> EmbeddingBatchResult:
        """Embed texts and return vectors plus aggregated provider-reported usage.

        Position-preserving like ``get_embeddings``: empty/whitespace/None
        inputs are not sent to the API and stay as ``None`` at their original
        positions in ``embeddings``. Usage fields fall back to ``None`` and
        ``cost_source="unknown"`` for providers/transports that don't report
        them (e.g. Gemini-native).
        """
        if not texts:
            return EmbeddingBatchResult(embeddings=[])

        pairs = [(i, t) for i, t in enumerate(texts) if t is not None and t.strip() != ""]

        result: List[Optional[List[float]]] = [None] * len(texts)

        if not pairs:
            logger.warning("All texts are empty, returning aligned list of None")
            return EmbeddingBatchResult(embeddings=result)

        if len(pairs) != len(texts):
            logger.warning(f"Filtered out {len(texts) - len(pairs)} empty texts (positions preserved as None)")

        non_empty_texts = [t for _, t in pairs]

        try:
            embeddings, usage, api_calls = self._embed_with_retries(non_empty_texts, format_spec)
        except Exception as e:
            if self._is_input_too_long_error(e):
                logger.warning(
                    f"Embedding batch hit input-too-long ({len(non_empty_texts)} texts); "
                    f"falling back to binary partition split"
                )
                embeddings, usage, api_calls = self._handle_input_too_long(non_empty_texts, format_spec)
            else:
                raise

        for k, emb in enumerate(embeddings):
            orig_idx = pairs[k][0]
            result[orig_idx] = emb
        logger.debug(f"Generated {len(non_empty_texts)} embeddings (result length={len(result)})")

        return _build_batch_result(result, usage, api_calls=api_calls)

    def _embed_with_retries(
        self, raw_texts: List[str], format_spec: EmbeddingFormatSpec
    ) -> Tuple[List[List[float]], UsageDict, int]:
        """
        Apply format_spec, dispatch to the chosen transport sender, and retry on transient failures.
        Returns embeddings aligned with `raw_texts`, an optional usage dict
        (``{prompt_tokens, total_tokens, cost, cost_unit}`` or None), and the number of
        successful API calls made for this batch (1 normally, 2 when the no-data float
        fallback recovered the batch inside a single attempt).
        """
        prepared = [format_spec.format_text(t) for t in raw_texts]
        request_options = format_spec.request_options
        last_err = None
        for attempt in range(1, max(1, self.max_retries) + 1):
            try:
                if self.transport == "gemini_native":
                    vectors, usage = self._send_batch_gemini_native(
                        prepared, request_options.get("taskType")
                    )
                    api_calls = 1
                else:
                    vectors, usage, api_calls = self._send_openai_with_float_fallback(
                        prepared, request_options.get("extra_body"), attempt
                    )
                if len(vectors) != len(prepared):
                    raise RuntimeError(
                        f"Embedding API returned {len(vectors)} items "
                        f"for {len(prepared)} non-empty inputs"
                    )
                return vectors, usage, api_calls
            except Exception as e:
                last_err = e
                if attempt >= max(1, self.max_retries) or not self._is_transient_error(e):
                    if is_embedding_unavailable_error(e):
                        # Expected external endpoint degradation (e.g. provider 200
                        # with empty data). One quiet line, no traceback — the caller
                        # classifies this as an outage, not a code bug.
                        logger.warning(
                            "Embedding batch request unavailable after %d attempts "
                            "for %d texts: %s: %s",
                            attempt, len(prepared), type(e).__name__, e,
                        )
                    else:
                        logger.error(f"Failed to get embeddings for {len(prepared)} texts: {e}")
                    break
                logger.warning(f"Embedding batch request failed (attempt {attempt}/{self.max_retries}), retrying: {e}")
                self._sleep_backoff(attempt)
        raise last_err

    def _send_openai_with_float_fallback(
        self, prepared: List[str], extra_body: Optional[Dict[str, Any]], attempt: int
    ) -> Tuple[List[List[float]], UsageDict, int]:
        """Send one OpenAI-compatible batch; on ``No embedding data received`` retry the same
        batch once with ``encoding_format="float"`` inside this attempt (no backoff, no extra
        retry-attempt). Returns (vectors, usage, api_calls) — api_calls is 2 when the float
        fallback recovered the batch, else 1.

        If the fallback itself fails, the original no-data error is re-raised so the outer
        retry classification stays unchanged and the next attempt starts from the default call.
        """
        try:
            vectors, usage = self._send_batch_openai_compatible(prepared, extra_body)
            return vectors, usage, 1
        except Exception as primary_err:
            if not (
                getattr(self, "float_fallback_on_no_data", True)
                and _is_no_embedding_data_error(primary_err)
            ):
                raise
            logger.warning(
                "Embedding batch returned no data (attempt %d/%d, %d texts); "
                "retrying batch with encoding_format=float",
                attempt, max(1, self.max_retries), len(prepared),
            )
            try:
                vectors, usage = self._send_batch_openai_compatible(
                    prepared, extra_body, encoding_format="float"
                )
            except Exception as fb_err:
                logger.warning(
                    "encoding_format=float fallback also failed (%s: %s); "
                    "continuing retry on the original no-data error",
                    type(fb_err).__name__, fb_err,
                )
                raise primary_err
            logger.debug("encoding_format=float fallback recovered the embedding batch")
            return vectors, usage, 2

    def get_embeddings_batched(
        self, texts: List[str], *, format_spec: EmbeddingFormatSpec
    ) -> List[Optional[List[float]]]:
        """
        Get embeddings for multiple texts, automatically batching if needed.

        Returns embeddings aligned with input positions; None for empty inputs.
        """
        return self.get_embeddings_batched_with_usage(texts, format_spec=format_spec).embeddings

    def get_embeddings_batched_with_usage(
        self, texts: List[str], *, format_spec: EmbeddingFormatSpec
    ) -> EmbeddingBatchResult:
        """
        Get embeddings for multiple texts, automatically batching if needed,
        and return aggregated provider-reported usage.
        """
        if not texts:
            return EmbeddingBatchResult(embeddings=[])

        if len(texts) <= self.batch_size:
            return self.get_embeddings_with_usage(texts, format_spec=format_spec)

        all_embeddings: List[Optional[List[float]]] = []
        total_usage: UsageDict = None
        api_calls = 0
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            batch_result = self.get_embeddings_with_usage(batch, format_spec=format_spec)
            all_embeddings.extend(batch_result.embeddings)
            total_usage = _merge_usage(total_usage, _usage_from_batch_result(batch_result))
            api_calls += max(0, int(batch_result.api_calls or 0))
            logger.debug(f"Processed batch {i // self.batch_size + 1}/{(len(texts) + self.batch_size - 1) // self.batch_size}")

        return _build_batch_result(all_embeddings, total_usage, api_calls=api_calls)

    def _send_batch_openai_compatible(
        self,
        prepared_texts: List[str],
        extra_body: Optional[Dict[str, Any]],
        encoding_format: Optional[str] = None,
    ) -> Tuple[List[List[float]], UsageDict]:
        """Single OpenAI-compatible batch call. No retry, no input-too-long handling here.

        `encoding_format` is only passed through when explicitly set (the no-data float
        fallback); the default path omits it to preserve current SDK/default behavior.
        """
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "input": prepared_texts,
            "timeout": self.timeout_seconds,
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        if encoding_format is not None:
            kwargs["encoding_format"] = encoding_format
        response = self.client.embeddings.create(**kwargs)
        vectors = [self._decode_embedding_item(item) for item in response.data]
        usage = _extract_openai_usage(response)
        return vectors, usage

    def _send_batch_gemini_native(
        self, prepared_texts: List[str], task_type: Optional[str]
    ) -> Tuple[List[List[float]], UsageDict]:
        """
        Single Gemini-native batch call via REST :batchEmbedContents.
        No retry, no input-too-long handling here. Alignment with prepared_texts is preserved.
        """
        import httpx
        if self._gemini_http_client is None:
            try:
                import openai as _openai
                self._gemini_http_client = (
                    _openai.DefaultHttpxClient(proxy=self._gemini_proxy)
                    if self._gemini_proxy else httpx.Client()
                )
            except Exception:
                self._gemini_http_client = httpx.Client()

        model_path = self.model if self.model.startswith("models/") else f"models/{self.model}"
        url = f"{self.api_base.rstrip('/')}/{model_path}:batchEmbedContents"
        requests_body = []
        for t in prepared_texts:
            req: Dict[str, Any] = {
                "model": model_path,
                "content": {"parts": [{"text": t}]},
            }
            if task_type:
                req["taskType"] = task_type
            requests_body.append(req)

        headers = {"x-goog-api-key": self.api_key or "", "Content-Type": "application/json"}
        resp = self._gemini_http_client.post(
            url, json={"requests": requests_body}, headers=headers, timeout=self.timeout_seconds
        )
        resp.raise_for_status()
        data = resp.json()
        embeddings = data.get("embeddings") or []
        return [list(item.get("values") or []) for item in embeddings], None

    def _decode_embedding_item(self, item: Any) -> List[float]:
        """
        Normalize a single embedding item to List[float].
        Perplexity returns base64-int8 strings; everything else returns native lists.
        """
        emb = getattr(item, "embedding", None)
        if emb is None and isinstance(item, dict):
            emb = item.get("embedding")
        if isinstance(emb, str) and self.text_format_profile == "perplexity":
            raw = base64.b64decode(emb)
            vec = [float(b if b < 128 else b - 256) / 127.0 for b in raw]
            norm = math.sqrt(sum(x * x for x in vec))
            return [x / norm for x in vec] if norm > 0.0 else vec
        return list(emb) if emb is not None else []

    def _sleep_backoff(self, attempt: int) -> None:
        """
        Sleep with exponential backoff and small jitter for transient failures.
        """
        try:
            base = float(self.backoff_base) if self.backoff_base and self.backoff_base > 0 else 0.5
            cap = float(self.backoff_cap) if self.backoff_cap and self.backoff_cap > 0 else 4.0
            jitter = float(self.backoff_jitter) if self.backoff_jitter and self.backoff_jitter >= 0 else 0.2
            delay = min(cap, base * (2 ** max(0, attempt - 1))) + random.uniform(0.0, jitter)
            time.sleep(delay)
        except Exception:
            # As a last resort, avoid raising from sleep helpers
            time.sleep(0.1)

    def _extract_http_status(self, err: Exception) -> Optional[int]:
        """Instance delegate to the module-level status extractor."""
        return _extract_http_status(err)

    def _error_message(self, err: Exception) -> str:
        """Instance delegate to the module-level error-message combiner."""
        return _error_message(err)

    def _is_input_too_long_error(self, err: Exception) -> bool:
        """Instance delegate to the module-level input-too-long classifier."""
        return _is_input_too_long(err)

    def _handle_input_too_long(
        self, texts: List[str], format_spec: EmbeddingFormatSpec
    ) -> Tuple[List[List[float]], UsageDict, int]:
        """
        Binary partition isolate-first fallback for input-too-long errors.
        `texts` are raw (unformatted) — format_spec is reapplied on every recursion level
        so prefixed profiles keep their prefix in each subchunk.
        Also returns the total number of successful API calls made across the split.
        """
        if not texts:
            return [], None, 0

        if len(texts) == 1:
            chunk_chars = int(getattr(self, "effective_chunk_chars", 0) or 0)
            if chunk_chars <= 0:
                chunk_chars = max(_ADAPTIVE_MIN_CHARS, len(texts[0]) // 2 or _ADAPTIVE_MIN_CHARS)
            single_vec, single_usage, single_calls = self._embed_single_with_split(
                texts[0], chunk_chars, depth=0, format_spec=format_spec,
            )
            return [single_vec], single_usage, single_calls

        mid = len(texts) // 2
        left = texts[:mid]
        right = texts[mid:]

        left_emb, left_usage, left_calls = self._embed_or_handle_too_long(left, format_spec)
        right_emb, right_usage, right_calls = self._embed_or_handle_too_long(right, format_spec)
        return left_emb + right_emb, _merge_usage(left_usage, right_usage), left_calls + right_calls

    def _embed_or_handle_too_long(
        self, texts: List[str], format_spec: EmbeddingFormatSpec
    ) -> Tuple[List[List[float]], UsageDict, int]:
        """Try a normal batched embed; on input-too-long fall back to binary partition."""
        if not texts:
            return [], None, 0
        try:
            return self._embed_with_retries(texts, format_spec)
        except Exception as e:
            if self._is_input_too_long_error(e):
                return self._handle_input_too_long(texts, format_spec)
            raise

    def _embed_single_with_split(
        self, text: str, max_chars: int, depth: int, format_spec: EmbeddingFormatSpec
    ) -> Tuple[List[float], UsageDict, int]:
        """
        Split a single text into subchunks of `max_chars`, embed them and pool to one vector.
        Recurse with `max_chars // 2` if the API still rejects subchunks as too long.
        Raises if max depth is reached or min chunk size is hit without success.
        Returns the pooled vector, aggregated usage, and the number of successful API calls
        across all sub-batches.
        """
        if depth >= _ADAPTIVE_MAX_DEPTH:
            raise RuntimeError(
                f"Adaptive split exhausted max_depth={_ADAPTIVE_MAX_DEPTH} for text len={len(text)}"
            )
        if max_chars < _ADAPTIVE_MIN_CHARS:
            raise RuntimeError(
                f"Adaptive split reached min_adaptive_chars={_ADAPTIVE_MIN_CHARS}; cannot reduce further"
            )

        subchunks, lengths = split_text_for_embedding(
            text,
            max_chars=max_chars,
            overlap_chars=_ADAPTIVE_OVERLAP,
            max_chunks=_ADAPTIVE_MAX_SUBCHUNKS,
        )
        if not subchunks:
            raise RuntimeError("Adaptive split produced no subchunks")

        total_usage: UsageDict = None
        total_calls = 0
        try:
            vectors: List[List[float]] = []
            slice_size = max(1, int(self.batch_size))
            for i in range(0, len(subchunks), slice_size):
                slice_ = subchunks[i:i + slice_size]
                vecs, slice_usage, slice_calls = self._embed_with_retries(slice_, format_spec)
                vectors.extend(vecs)
                total_usage = _merge_usage(total_usage, slice_usage)
                total_calls += slice_calls
        except Exception as e:
            if self._is_input_too_long_error(e):
                logger.warning(
                    f"Adaptive split depth={depth} max_chars={max_chars} still hit input-too-long; "
                    f"recursing with max_chars={max_chars // 2}"
                )
                return self._embed_single_with_split(text, max_chars // 2, depth + 1, format_spec)
            raise

        l2_chunks = bool(getattr(settings, "embedding_l2_norm_chunks", True))
        l2_final = bool(getattr(settings, "embedding_l2_norm_final", True))
        return (
            weighted_mean_pool(vectors, lengths, l2_chunks=l2_chunks, l2_final=l2_final),
            total_usage,
            total_calls,
        )

    def _is_transient_error(self, err: Exception) -> bool:
        """
        Heuristic detection of transient/network/429/5xx errors for retry purposes.
        Chain-aware via the shared ``_matches_outage_signatures`` so retry and the
        quiet-degradation classifier (``is_embedding_unavailable_error``) stay in
        sync. Handles OpenAI SDK exceptions (status on the exception itself),
        httpx.HTTPStatusError (status on err.response) and wrapped connection
        drops (status/markers found on a ``__cause__``/``__context__`` link).
        """
        return _matches_outage_signatures(err)


