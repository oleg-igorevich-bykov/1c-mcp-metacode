"""Dedicated LLM channel for object_summary.

This client speaks the OpenAI-compatible Chat Completions API and is wired
exclusively to `OBJECT_SUMMARY_LLM_*` settings — no fallback on the shared
`EMBEDDING_*` or BSL-rerank configuration. Isolation invariant: changing
the embedding provider must not silently change the summary generator.

Retries use exponential backoff with a small jitter, identical to the
embedding service. The response is requested in `json_object` mode and then
fed to `contract.validate_summary` for normalisation.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from config import settings

from .contract import (
    RESPONSE_FORMAT,
    SYSTEM_PROMPT,
    ValidatedSummary,
    build_user_prompt,
    validate_summary,
)

logger = logging.getLogger(__name__)


class ObjectSummaryLLMError(RuntimeError):
    """Raised when the LLM call could not be completed within the retry budget."""


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResult:
    summary: ValidatedSummary
    usage: LLMUsage
    elapsed_seconds: float
    model: str
    provider: str = "unknown"
    cost_amount: Optional[float] = None
    cost_unit: Optional[str] = None
    cost_source: str = "unknown"


def _detect_provider(api_base: str) -> str:
    """Map the LLM endpoint host to a coarse provider tag used for usage aggregates.

    Only `openrouter` is treated as a source of provider-reported cost
    (its OpenAI-compatible responses carry an extra `usage.cost`).
    """
    host = (urlparse(api_base).hostname or "").lower()
    if not host:
        return "unknown"
    if host.endswith("openrouter.ai"):
        return "openrouter"
    if host.endswith("api.openai.com") or host.endswith(".openai.com"):
        return "openai"
    if host.endswith("deepseek.com"):
        return "deepseek"
    return "unknown"


class ObjectSummaryLLM:
    """Thin OpenAI-compatible wrapper around chat.completions.create."""

    def __init__(self) -> None:
        if not settings.object_summary_llm_api_base:
            raise ValueError(
                "OBJECT_SUMMARY_LLM_API_BASE is not configured; object summary feature requires "
                "an explicit LLM endpoint and will not fall back to EMBEDDING_*."
            )
        api_base = (settings.object_summary_llm_api_base or "").strip()
        if not api_base.startswith(("http://", "https://")):
            raise ValueError(
                f"Invalid OBJECT_SUMMARY_LLM_API_BASE={api_base!r}; expected http(s):// URL."
            )

        self.model: str = (settings.object_summary_model or "").strip() or "gpt-4o-mini"
        self.api_base: str = api_base
        self.provider: str = _detect_provider(api_base)
        self.api_key: str = (settings.object_summary_llm_api_key or "").strip()
        self.timeout: float = float(settings.object_summary_llm_timeout or 300.0)
        self.temperature: float = float(settings.object_summary_llm_temperature or 0.0)
        self.max_retries: int = max(1, int(settings.object_summary_generation_max_retries or 3))

        try:
            import openai  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                f"openai package not installed: {exc}; required for object summary LLM"
            ) from exc

        import openai
        proxy = (settings.object_summary_llm_proxy or "").strip() or None
        http_client = openai.DefaultHttpxClient(proxy=proxy) if proxy else None
        self._client = openai.OpenAI(
            api_key=self.api_key or "sk-no-key",
            base_url=self.api_base,
            **({"http_client": http_client} if http_client else {}),
        )

        logger.info(
            "ObjectSummaryLLM initialised: model=%s api_base=%s timeout=%.1fs temperature=%.2f",
            self.model, self.api_base, self.timeout, self.temperature,
        )

    def _is_transient(self, exc: Exception) -> bool:
        text = repr(exc).lower()
        return any(
            tok in text
            for tok in ("rate limit", "timeout", "timed out", "temporar", "overload", "connection", "503", "502", "504")
        )

    def _sleep_backoff(self, attempt: int) -> None:
        base = float(settings.embedding_retry_backoff_base_seconds or 0.5)
        cap = float(settings.embedding_retry_backoff_max_seconds or 4.0)
        jitter = float(settings.embedding_retry_jitter_seconds or 0.2)
        delay = min(cap, base * (2 ** max(0, attempt - 1)))
        delay += random.uniform(0.0, jitter)
        time.sleep(delay)

    def generate(self, profile_text: str, *, profile_format: str = "toon") -> LLMResult:
        """Synchronously generate and validate a summary from a TOON-encoded profile."""
        prompt = build_user_prompt(profile_text, profile_format=profile_format)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        last_error: Optional[Exception] = None
        started = time.perf_counter()
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    timeout=self.timeout,
                    response_format=RESPONSE_FORMAT,
                )
                break
            except Exception as exc:  # broad: openai SDK exception hierarchy varies by version
                last_error = exc
                if attempt >= self.max_retries or not self._is_transient(exc):
                    logger.error("ObjectSummaryLLM call failed (attempt %d/%d): %s", attempt, self.max_retries, exc)
                    raise ObjectSummaryLLMError(str(exc)) from exc
                logger.warning(
                    "ObjectSummaryLLM transient error (attempt %d/%d), retrying: %s",
                    attempt, self.max_retries, exc,
                )
                self._sleep_backoff(attempt)
        else:  # pragma: no cover - defensive
            raise ObjectSummaryLLMError(str(last_error) if last_error else "unknown error")

        elapsed = time.perf_counter() - started

        try:
            content = response.choices[0].message.content or ""
        except (AttributeError, IndexError) as exc:
            raise ObjectSummaryLLMError(f"LLM response has no choices: {exc}") from exc

        validated = validate_summary(content)

        usage = LLMUsage()
        cost_amount: Optional[float] = None
        cost_unit: Optional[str] = None
        cost_source = "unknown"
        try:
            raw_usage = getattr(response, "usage", None)
            if raw_usage is not None:
                usage.prompt_tokens = int(getattr(raw_usage, "prompt_tokens", 0) or 0)
                usage.completion_tokens = int(getattr(raw_usage, "completion_tokens", 0) or 0)
                usage.total_tokens = int(getattr(raw_usage, "total_tokens", 0) or 0)
                if self.provider == "openrouter":
                    raw_cost = getattr(raw_usage, "cost", None)
                    if raw_cost is None and isinstance(raw_usage, dict):
                        raw_cost = raw_usage.get("cost")
                    if isinstance(raw_cost, (int, float)):
                        cost_amount = float(raw_cost)
                        cost_unit = "credits"
                        cost_source = "provider_reported"
        except (TypeError, ValueError):
            pass

        return LLMResult(
            summary=validated,
            usage=usage,
            elapsed_seconds=elapsed,
            model=self.model,
            provider=self.provider,
            cost_amount=cost_amount,
            cost_unit=cost_unit,
            cost_source=cost_source,
        )


_SINGLETON: Optional[ObjectSummaryLLM] = None


def get_object_summary_llm() -> Optional[ObjectSummaryLLM]:
    """Return a process-wide LLM client, or `None` if disabled / misconfigured."""
    global _SINGLETON
    if not settings.object_summary_enabled:
        return None
    if _SINGLETON is not None:
        return _SINGLETON
    try:
        _SINGLETON = ObjectSummaryLLM()
    except Exception as exc:
        logger.warning("ObjectSummaryLLM is unavailable: %s", exc)
        return None
    return _SINGLETON
