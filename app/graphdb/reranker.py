"""
Shared cross-encoder reranker HTTP client (Cohere/OpenRouter-compatible /rerank API).

Consumers (BSL code search, object summary search) share one singleton via
`get_reranker()`. Each consumer decides on its own whether rerank is enabled
(`*_RERANK_ENABLED`) and how many candidates to send (`*_RERANK_TOP_K`);
this module owns only the transport, retry policy and response parsing.

Activated only when `settings.rerank_api_key` is not empty. Any failure inside
`rerank(...)` is swallowed (warning log) and returned as None so callers can
silently fall back to the current order.

Uses urllib.request from stdlib (no extra dependency). Tests inject a fake
opener via `_http_post` for deterministic transport.
"""
from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request
from typing import Any, List, Optional, Tuple

from config import settings
import runtime_metrics

logger = logging.getLogger(__name__)


_instance: Optional["Reranker"] = None
_checked: bool = False


def get_reranker() -> Optional["Reranker"]:
    """Singleton accessor. Returns None when rerank is disabled or misconfigured."""
    global _instance, _checked
    if _checked:
        return _instance
    _checked = True
    api_key = (getattr(settings, "rerank_api_key", "") or "").strip()
    if not api_key:
        logger.info("Reranker disabled: rerank_api_key is empty")
        _instance = None
        return None
    try:
        _instance = Reranker()
    except Exception as e:
        logger.warning("Reranker init failed: %s", e)
        _instance = None
    return _instance


def reset_reranker_singleton() -> None:
    """Test hook: clear the singleton so settings overrides take effect."""
    global _instance, _checked
    _instance = None
    _checked = False


class _HttpResponse:
    """Minimal HTTP response container the rerank loop operates on."""

    __slots__ = ("status_code", "body", "_json_cache")

    def __init__(self, status_code: int, body: bytes):
        self.status_code = int(status_code)
        self.body = body or b""
        self._json_cache: Any = _UNSET

    @property
    def text(self) -> str:
        try:
            return self.body.decode("utf-8", errors="replace")
        except Exception:
            return ""

    def json(self) -> Any:
        if self._json_cache is _UNSET:
            self._json_cache = json.loads(self.body.decode("utf-8"))
        return self._json_cache


_UNSET = object()


class _NetworkError(Exception):
    """Wraps transient transport errors (timeouts, connection failures) for retry."""


def _http_post(
    url: str,
    payload: dict,
    headers: dict,
    timeout: float,
    proxy: Optional[str],
) -> _HttpResponse:
    """
    Synchronous JSON POST via urllib. Raises _NetworkError for transport-level
    failures; returns an _HttpResponse for any HTTP status (including 4xx/5xx).
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url=url, data=data, headers=headers, method="POST")
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    else:
        opener = urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read()
            status = getattr(resp, "status", None) or resp.getcode() or 200
            return _HttpResponse(status, body)
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return _HttpResponse(e.code, body)
    except (socket.timeout, TimeoutError) as e:
        raise _NetworkError(f"timeout: {e}") from e
    except urllib.error.URLError as e:
        raise _NetworkError(f"transport: {e}") from e


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1000))


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _first_int(mapping: dict[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        value = _coerce_int(mapping.get(key))
        if value is not None:
            return value
    return None


def _first_float(mapping: dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = _coerce_float(mapping.get(key))
        if value is not None:
            return value
    return None


def _extract_rerank_usage(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {
            "input_tokens": None,
            "total_tokens": None,
            "cost_amount": None,
            "cost_unit": None,
            "cost_source": "unknown",
        }

    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}

    input_tokens = _first_int(usage, "input_tokens", "prompt_tokens")
    total_tokens = _first_int(usage, "total_tokens")
    cost_amount = _first_float(usage, "cost", "total_cost")
    if cost_amount is None:
        cost_amount = _first_float(data, "cost", "total_cost")

    cost_unit = (
        str(
            usage.get("cost_unit")
            or usage.get("currency")
            or data.get("cost_unit")
            or data.get("currency")
            or ""
        ).strip()
        or None
    )
    if cost_amount is not None and not cost_unit:
        cost_unit = "usd"

    return {
        "input_tokens": input_tokens,
        "total_tokens": total_tokens,
        "cost_amount": cost_amount,
        "cost_unit": cost_unit,
        "cost_source": "provider_reported" if cost_amount is not None else "unknown",
    }


class Reranker:
    """Cohere/OpenRouter-compatible /rerank client."""

    def __init__(self) -> None:
        self.model: str = (getattr(settings, "rerank_model", "") or "").strip()
        self.api_base: str = (getattr(settings, "rerank_api_base", "") or "").strip()
        self.api_key: str = (getattr(settings, "rerank_api_key", "") or "").strip()
        self.proxy: Optional[str] = getattr(settings, "rerank_proxy", None) or None
        self.timeout_seconds: float = float(getattr(settings, "rerank_timeout_seconds", 60.0) or 60.0)
        self.max_retries: int = max(1, int(getattr(settings, "rerank_max_retries", 2) or 2))

        if not self.model:
            raise ValueError("rerank_model is empty")
        if not self.api_base:
            raise ValueError("rerank_api_base is empty")
        if not (self.api_base.startswith("http://") or self.api_base.startswith("https://")):
            raise ValueError(
                f"Invalid rerank_api_base: '{self.api_base}'. "
                "URL must start with 'http://' or 'https://'."
            )

        self._endpoint = self.api_base.rstrip("/") + "/rerank"
        self._post = _http_post

        logger.info(
            "Reranker initialized: model=%s, endpoint=%s, timeout=%.1fs, retries=%d, proxy=%s",
            self.model, self._endpoint, self.timeout_seconds, self.max_retries,
            "configured" if self.proxy else "none",
        )

    def rerank(
        self,
        query: str,
        documents: List[str],
        top_n: int,
        *,
        event_type: str = "rerank",
    ) -> Optional[List[Tuple[int, float]]]:
        """
        Call the /rerank endpoint and return [(original_index, relevance_score), ...]
        sorted by relevance_score DESC, capped at `top_n`.

        Returns None on any error (network, non-2xx, malformed JSON, empty results).
        Caller must treat None as 'rerank unavailable, use current order'.

        Retry policy: transient errors (network, 5xx) get retried up to max_retries;
        4xx is treated as configuration error and not retried.
        """
        if not query or not documents:
            return None
        if int(top_n) <= 0:
            return None

        payload = {
            "model": self.model,
            "query": query,
            "documents": list(documents),
            "top_n": int(top_n),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_err: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            attempt_started = time.perf_counter()
            try:
                resp = self._post(
                    self._endpoint, payload, headers,
                    self.timeout_seconds, self.proxy,
                )
            except _NetworkError as e:
                self._record_usage_attempt(
                    event_type=event_type,
                    success=False,
                    duration_ms=_elapsed_ms(attempt_started),
                )
                last_err = e
                logger.warning(
                    "Reranker network error (attempt %d/%d): %s",
                    attempt, self.max_retries, e,
                )
                if attempt >= self.max_retries:
                    return None
                _sleep_backoff(attempt)
                continue
            except Exception as e:
                self._record_usage_attempt(
                    event_type=event_type,
                    success=False,
                    duration_ms=_elapsed_ms(attempt_started),
                )
                logger.warning("Reranker unexpected error: %s", e)
                return None

            status = resp.status_code
            if 200 <= status < 300:
                try:
                    data = resp.json()
                except Exception as e:
                    self._record_usage_attempt(
                        event_type=event_type,
                        success=False,
                        duration_ms=_elapsed_ms(attempt_started),
                    )
                    logger.warning("Reranker malformed JSON: %s", e)
                    return None
                parsed = _parse_rerank_response(data, len(documents))
                if not parsed:
                    self._record_usage_attempt(
                        event_type=event_type,
                        success=False,
                        duration_ms=_elapsed_ms(attempt_started),
                        data=data,
                    )
                    logger.warning("Reranker returned empty/invalid results")
                    return None
                parsed.sort(key=lambda it: it[1], reverse=True)
                self._record_usage_attempt(
                    event_type=event_type,
                    success=True,
                    duration_ms=_elapsed_ms(attempt_started),
                    data=data,
                )
                return parsed[: int(top_n)]

            if 500 <= status < 600:
                self._record_usage_attempt(
                    event_type=event_type,
                    success=False,
                    duration_ms=_elapsed_ms(attempt_started),
                    data=_try_response_json(resp),
                )
                last_err = RuntimeError(f"HTTP {status}: {resp.text[:200]}")
                logger.warning(
                    "Reranker 5xx (attempt %d/%d): %s",
                    attempt, self.max_retries, last_err,
                )
                if attempt >= self.max_retries:
                    return None
                _sleep_backoff(attempt)
                continue

            # 4xx — configuration/auth error; do not retry.
            self._record_usage_attempt(
                event_type=event_type,
                success=False,
                duration_ms=_elapsed_ms(attempt_started),
                data=_try_response_json(resp),
            )
            logger.warning(
                "Reranker non-retriable HTTP %d: %s",
                status, (resp.text or "")[:200],
            )
            return None

        if last_err is not None:
            logger.warning("Reranker giving up after retries: %s", last_err)
        return None

    def _record_usage_attempt(
        self,
        *,
        event_type: str,
        success: bool,
        duration_ms: int,
        data: Any = None,
    ) -> None:
        try:
            usage = _extract_rerank_usage(data)
            runtime_metrics.record_rerank_usage(
                event_type=event_type,
                provider=runtime_metrics.detect_provider_from_api_base(
                    self.api_base, fallback="unknown",
                ),
                model=self.model,
                calls=1,
                success=success,
                input_tokens=usage["input_tokens"],
                total_tokens=usage["total_tokens"],
                cost_amount=usage["cost_amount"],
                cost_unit=usage["cost_unit"],
                cost_source=usage["cost_source"],
                duration_ms=duration_ms,
            )
        except Exception as exc:
            logger.debug("Reranker usage metric skipped: %s", exc)


def _sleep_backoff(attempt: int) -> None:
    delay = min(1.0, 0.25 * (2 ** (attempt - 1)))
    time.sleep(delay)


def _try_response_json(resp: _HttpResponse) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def _parse_rerank_response(data, n_documents: int) -> List[Tuple[int, float]]:
    """
    Accept either {"results": [{"index": i, "relevance_score": s}, ...]}
    or {"data": [{"index": i, "relevance_score": s}, ...]}.
    Fallback key for score: "score". Indices outside [0, n_documents) are dropped.
    """
    if not isinstance(data, dict):
        return []
    items = data.get("results")
    if not isinstance(items, list):
        items = data.get("data")
    if not isinstance(items, list):
        return []
    out: List[Tuple[int, float]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        score = item.get("relevance_score")
        if score is None:
            score = item.get("score")
        if idx is None or score is None:
            continue
        try:
            i = int(idx)
            s = float(score)
        except (TypeError, ValueError):
            continue
        if i < 0 or i >= n_documents:
            continue
        out.append((i, s))
    return out
