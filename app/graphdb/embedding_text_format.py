"""
Embedding input formatting per model profile.

Exposes a single immutable contract (EmbeddingFormatSpec) that carries both the
text wrapper and the transport-specific request options for a given
(profile, transport, side, purpose) tuple. Callers must use
build_embedding_format_spec() to construct a spec — lower-level helpers are
intentionally private.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, Literal


BSL_CODE_EMBEDDING_FORMAT_VERSION = 1


EmbeddingSide = Literal["query", "document"]
EmbeddingPurpose = Literal["description", "code"]


_KNOWN_PROFILES = (
    "openai", "codestral", "perplexity",
    "nomic_text", "nomic_code",
    "qwen3", "f2llm_v2", "harrier", "bge_m3",
    "e5", "jina_v4", "jina_v5", "jina_code",
    "coderankembed",
    "gemini", "none",
)

_KNOWN_TRANSPORTS = ("openai_compatible", "openrouter", "gemini_native")


# Fixed per-model code-search prompts taken verbatim from the model cards.
# These are requirements of the model itself, not configurable knobs.
_F2LLM_HARRIER_QWEN3_CODE_QUERY_INSTRUCTION = (
    "Given a question about code, retrieve the relevant code snippet "
    "that answers the question."
)
_NOMIC_CODE_QUERY_PREFIX = "Represent this query for searching relevant code: "
_CODERANKEMBED_QUERY_PREFIX = "Represent this query for searching relevant code: "


def detect_embedding_text_profile(model_name: str) -> str:
    """
    Detect the formatting profile by substring-matching the model name.
    Returns "none" for unknown models — no prefix is applied.
    """
    if not model_name:
        return "none"
    m = model_name.lower()

    # Order: most specific substrings first to avoid collisions.
    if "jina-code-embeddings" in m:
        return "jina_code"
    if "jina-embeddings-v5" in m:
        return "jina_v5"
    if "jina-embeddings-v4" in m:
        return "jina_v4"
    if "nomic-embed-code" in m:
        return "nomic_code"
    if "nomic-embed-text" in m:
        return "nomic_text"
    if "coderankembed" in m:
        return "coderankembed"
    if "qwen3-embedding" in m or "text-embedding-qwen3" in m:
        return "qwen3"
    if "f2llm-v2" in m:
        return "f2llm_v2"
    if "harrier-oss-v1" in m:
        return "harrier"
    if "bge-m3" in m:
        return "bge_m3"
    if "intfloat/e5" in m or m.startswith("e5-") or "/e5-" in m:
        return "e5"
    if "gemini-embedding" in m:
        return "gemini"
    if "pplx-embed-v1" in m:
        return "perplexity"
    if "codestral-embed" in m:
        return "codestral"
    if "text-embedding-3-small" in m or "text-embedding-3-large" in m:
        return "openai"
    return "none"


def detect_embedding_transport(api_base: str) -> str:
    """
    Detect transport by api_base substring. Defaults to "openai_compatible".
    """
    if not api_base:
        return "openai_compatible"
    b = api_base.lower()
    if "openrouter.ai" in b:
        return "openrouter"
    if "generativelanguage.googleapis.com" in b or "aiplatform.googleapis.com" in b:
        return "gemini_native"
    return "openai_compatible"


def resolve_effective_embedding_transport(
    embedding_api_base: str,
    embedding_transport_setting: str,
) -> str:
    """
    Mirror the resolution used by `EmbeddingService.__init__`: an explicit
    non-"auto" `EMBEDDING_TRANSPORT` setting wins over substring detection
    from `EMBEDDING_API_BASE`. Anything else (including missing/empty) means
    "auto" → fall back to `detect_embedding_transport(api_base)`.
    """
    cfg = (embedding_transport_setting or "auto").strip().lower()
    if cfg == "auto" or not cfg:
        return detect_embedding_transport(embedding_api_base or "")
    return cfg


# env-facing BSL code prompt mode -> internal profile name
_BSL_CODE_PROMPT_MODE_TO_PROFILE: Dict[str, str] = {
    "none": "none",
    "jina-code-nl2code": "jina_code",
    "jina-v5-retrieval": "jina_v5",
    "jina-v4-code": "jina_v4",
    "nomic-code-search": "nomic_code",
    "coderankembed-code-search": "coderankembed",
    "harrier-code-search": "harrier",
    "f2llm-code-search": "f2llm_v2",
    "qwen3-code-search": "qwen3",
}


def resolve_bsl_code_prompt_profile(model_name: str, env_mode: str) -> str:
    """
    Resolve BSL code embedding prompt mode (env-facing) into an internal profile
    name from _KNOWN_PROFILES.

    Prompt mode names are distinct from profile names; this function is the only
    bridge between the two namespaces. Returns a name that is safe to pass into
    build_embedding_format_spec(profile=...).
    """
    em = (env_mode or "auto").strip().lower()
    if em == "auto":
        return detect_embedding_text_profile(model_name)
    if em in _BSL_CODE_PROMPT_MODE_TO_PROFILE:
        return _BSL_CODE_PROMPT_MODE_TO_PROFILE[em]
    raise ValueError(
        f"Unknown BSL_CODE_EMBEDDING_PROMPT_MODE: {env_mode!r}. "
        f"Allowed: auto, {', '.join(sorted(_BSL_CODE_PROMPT_MODE_TO_PROFILE))}."
    )


def compute_bsl_code_embedding_fingerprint(
    embedding_model: str,
    embedding_prompt_mode: str,
    embedding_api_base: str,
    embedding_transport_setting: str,
) -> str:
    """
    Phase B embedding fingerprint: identifies the vector space produced by the
    current BSL code embedding contract. Includes model, effective prompt
    profile, the effective transport actually used by `EmbeddingService`
    (mirrors the resolution in `resolve_effective_embedding_transport`), and a
    format version constant for manual invalidation. Raw `embedding_api_base`
    is intentionally excluded — the transport derived from it is what
    determines vector-space identity.
    """
    try:
        effective_profile = resolve_bsl_code_prompt_profile(
            embedding_model or "",
            embedding_prompt_mode or "auto",
        )
    except ValueError:
        effective_profile = "<invalid>"
    transport = resolve_effective_embedding_transport(
        embedding_api_base or "",
        embedding_transport_setting or "auto",
    )
    payload = {
        "embedding_model": embedding_model or "",
        "effective_bsl_code_prompt_profile": effective_profile,
        "transport": transport,
        "bsl_code_embedding_format_version": BSL_CODE_EMBEDDING_FORMAT_VERSION,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _format_text_impl(
    text: str,
    *,
    profile: str,
    side: EmbeddingSide,
    purpose: EmbeddingPurpose,
    description_instruction: str,
) -> str:
    """Internal: compute the prepared text for one (profile, side, purpose) tuple."""
    if text is None:
        return text

    if profile in ("none", "openai", "codestral", "perplexity"):
        return text

    if profile == "nomic_text":
        return f"search_query: {text}" if side == "query" else f"search_document: {text}"

    if profile == "nomic_code":
        # query side gets a fixed model-card prefix for code retrieval;
        # document side has no prefix per the model card.
        if purpose == "code" and side == "query":
            return f"{_NOMIC_CODE_QUERY_PREFIX}{text}"
        return text

    if profile == "coderankembed":
        if purpose == "code" and side == "query":
            return f"{_CODERANKEMBED_QUERY_PREFIX}{text}"
        return text

    if profile == "qwen3":
        if side == "query":
            if purpose == "code":
                return (
                    f"Instruct: {_F2LLM_HARRIER_QWEN3_CODE_QUERY_INSTRUCTION}\n"
                    f"Query: {text}"
                )
            return f"Instruct: {description_instruction}\n Query:{text}"
        return text

    if profile in ("f2llm_v2", "harrier"):
        if side == "query":
            if purpose == "code":
                return (
                    f"Instruct: {_F2LLM_HARRIER_QWEN3_CODE_QUERY_INSTRUCTION}\n"
                    f"Query: {text}"
                )
            return f"Instruct: {description_instruction}\nQuery: {text}"
        return text

    if profile == "bge_m3":
        if side == "query" and purpose == "description":
            return f"Represent this sentence for searching relevant passages: {text}"
        return text

    if profile == "e5":
        return f"query: {text}" if side == "query" else f"passage: {text}"

    if profile == "jina_v4":
        return f"Query: {text}" if side == "query" else f"Passage: {text}"

    if profile == "jina_v5":
        return f"Query: {text}" if side == "query" else f"Document: {text}"

    if profile == "jina_code":
        if purpose != "code":
            return text
        if side == "query":
            return f"Find the most relevant code snippet given the following query:\n{text}"
        return f"Candidate code snippet:\n{text}"

    # gemini and any unknown profile fall through to raw text; transport-level
    # task_type hints are emitted by _request_options_impl below.
    return text


def _request_options_impl(
    *,
    profile: str,
    transport: str,
    side: EmbeddingSide,
    purpose: EmbeddingPurpose,
) -> Dict[str, object]:
    """Internal: compute the transport-level options dict for one tuple."""
    if profile != "gemini":
        return {}

    if transport == "openrouter":
        input_type = "search_query" if side == "query" else "search_document"
        return {"extra_body": {"input_type": input_type}}

    if transport == "gemini_native":
        if purpose == "code" and side == "query":
            task_type = "CODE_RETRIEVAL_QUERY"
        elif side == "query":
            task_type = "RETRIEVAL_QUERY"
        else:
            task_type = "RETRIEVAL_DOCUMENT"
        return {"taskType": task_type}

    # gemini profile on a plain openai_compatible transport — no transport hint available.
    return {}


@dataclass(frozen=True)
class EmbeddingFormatSpec:
    """
    Atomic embedding-input contract: formatter + transport options bound together.
    Construct via build_embedding_format_spec().
    """
    profile: str
    transport: str
    side: EmbeddingSide
    purpose: EmbeddingPurpose
    description_instruction: str

    def format_text(self, raw: str) -> str:
        return _format_text_impl(
            raw,
            profile=self.profile,
            side=self.side,
            purpose=self.purpose,
            description_instruction=self.description_instruction,
        )

    @property
    def request_options(self) -> Dict[str, object]:
        return _request_options_impl(
            profile=self.profile,
            transport=self.transport,
            side=self.side,
            purpose=self.purpose,
        )


def build_embedding_format_spec(
    *,
    profile: str,
    transport: str,
    side: EmbeddingSide,
    purpose: EmbeddingPurpose,
    description_instruction: str,
) -> EmbeddingFormatSpec:
    """
    Single public constructor for EmbeddingFormatSpec.
    Validates profile/transport against the known set so silent typos surface early.
    """
    if profile not in _KNOWN_PROFILES:
        raise ValueError(f"Unknown embedding text format profile: {profile!r}")
    if transport not in _KNOWN_TRANSPORTS:
        raise ValueError(f"Unknown embedding transport: {transport!r}")
    if side not in ("query", "document"):
        raise ValueError(f"Invalid embedding side: {side!r}")
    if purpose not in ("description", "code"):
        raise ValueError(f"Invalid embedding purpose: {purpose!r}")
    return EmbeddingFormatSpec(
        profile=profile,
        transport=transport,
        side=side,
        purpose=purpose,
        description_instruction=description_instruction or "",
    )
