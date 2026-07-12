"""LLM profile resolution for the console agent."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import settings

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - exercised only if dependency is missing
    yaml = None


_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ENDPOINT_KEYS = {"title", "api_base", "api_key", "timeout", "proxy"}
_PROFILE_KEYS = {
    "title",
    "endpoint",
    "model",
    "temperature",
    "reasoning_effort",
    "reasoning_summary",
    "show_reasoning",
}


class AgentLlmConfigError(RuntimeError):
    """Raised when LLM profile configuration is invalid or unavailable."""


class AgentLlmProfileNotFound(AgentLlmConfigError):
    """Raised when a requested LLM profile id is absent."""


@dataclass(frozen=True)
class AgentLlmProfile:
    id: str
    title: str
    mode: str
    endpoint_id: str
    endpoint_title: str
    model: str
    api_base: str | None
    api_key_env: str | None
    api_key: str
    timeout: float
    proxy: str | None
    temperature: float | None
    reasoning_effort: str | None
    reasoning_summary: str | None
    show_reasoning: bool | None

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "endpointId": self.endpoint_id,
            "endpointTitle": self.endpoint_title,
            "model": self.model,
        }

    def usage_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.id,
            "profile_title": self.title,
            "endpoint_id": self.endpoint_id,
            "endpoint_title": self.endpoint_title,
            "model": self.model,
        }

    @property
    def runtime_provider(self) -> str:
        if self.mode == "config_file":
            return self.endpoint_id
        return "single_env"


@dataclass(frozen=True)
class AgentLlmCatalog:
    mode: str
    default_profile_id: str
    profiles: tuple[AgentLlmProfile, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "defaultProfileId": self.default_profile_id,
            "profiles": [profile.public_dict() for profile in self.profiles],
        }

    def by_id(self) -> dict[str, AgentLlmProfile]:
        return {profile.id: profile for profile in self.profiles}


def _config_path() -> Path:
    return Path(settings.console_agent_llm_config_path)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _validate_id(kind: str, value: str) -> str:
    text = _clean_text(value)
    if not text or not _ID_RE.fullmatch(text):
        raise AgentLlmConfigError(f"{kind} id must match [A-Za-z0-9_.-]")
    return text


def _float_or_default(value: Any, default: float) -> float:
    if value is None or value == "":
        return float(default)
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise AgentLlmConfigError(f"Invalid numeric value: {value!r}") from exc
    return result


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise AgentLlmConfigError(f"Invalid numeric value: {value!r}") from exc


def _optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise AgentLlmConfigError(f"Invalid boolean value: {value!r}")


def _optional_text(value: Any) -> str | None:
    text = _clean_text(value)
    return text or None


def _validate_known_keys(scope: str, raw: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(str(key) for key in raw.keys() if str(key) not in allowed)
    if unknown:
        raise AgentLlmConfigError(f"Unknown {scope} keys: {', '.join(unknown)}")


def _validate_api_base(api_base: str | None, *, field: str) -> str | None:
    text = _optional_text(api_base)
    if text and not text.startswith(("http://", "https://")):
        raise AgentLlmConfigError(f"{field} must be an http(s) URL")
    return text


def _resolve_endpoint_proxy(endpoint_id: str, endpoint_raw: dict[str, Any]) -> str | None:
    if "proxy" not in endpoint_raw:
        return _optional_text(settings.console_agent_llm_proxy)
    raw_proxy = endpoint_raw.get("proxy")
    if raw_proxy is False or raw_proxy is None:
        return None
    proxy = _optional_text(raw_proxy)
    if not proxy:
        return None
    if not proxy.startswith(("http://", "https://", "socks5://", "socks5h://")):
        raise AgentLlmConfigError(f"endpoints.{endpoint_id}.proxy must be an http(s) or socks5 URL, false, or null")
    return proxy


def _single_env_catalog() -> AgentLlmCatalog:
    model = _clean_text(settings.console_agent_model)
    api_key = _clean_text(settings.console_agent_llm_api_key) or _clean_text(os.getenv("OPENAI_API_KEY"))
    api_key_env = "CONSOLE_AGENT_LLM_API_KEY" if _clean_text(settings.console_agent_llm_api_key) else "OPENAI_API_KEY"
    profile = AgentLlmProfile(
        id="default",
        title=model,
        mode="single_env",
        endpoint_id="default",
        endpoint_title="Default",
        model=model,
        api_base=_optional_text(settings.console_agent_llm_api_base),
        api_key_env=api_key_env,
        api_key=api_key,
        timeout=float(settings.console_agent_llm_timeout or 120.0),
        proxy=_optional_text(settings.console_agent_llm_proxy),
        temperature=float(settings.console_agent_llm_temperature or 0.0),
        reasoning_effort=_optional_text(settings.console_agent_reasoning_effort),
        reasoning_summary=_optional_text(settings.console_agent_reasoning_summary),
        show_reasoning=bool(settings.console_agent_show_reasoning),
    )
    return AgentLlmCatalog(mode="single_env", default_profile_id=profile.id, profiles=(profile,))


def _load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise AgentLlmConfigError("PyYAML is required to read console_agent_llm.yaml")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AgentLlmConfigError(f"Failed to read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AgentLlmConfigError("console_agent_llm.yaml must contain a YAML object")
    return payload


def _config_file_catalog(path: Path) -> AgentLlmCatalog:
    payload = _load_yaml(path)
    endpoints_raw = payload.get("endpoints")
    profiles_raw = payload.get("profiles")
    if not isinstance(endpoints_raw, dict) or not endpoints_raw:
        raise AgentLlmConfigError("console_agent_llm.yaml endpoints must be a non-empty object")
    if not isinstance(profiles_raw, dict) or not profiles_raw:
        raise AgentLlmConfigError("console_agent_llm.yaml profiles must be a non-empty object")

    endpoints: dict[str, dict[str, Any]] = {}
    for endpoint_id_raw, endpoint_raw in endpoints_raw.items():
        endpoint_id = _validate_id("endpoint", str(endpoint_id_raw))
        if not isinstance(endpoint_raw, dict):
            raise AgentLlmConfigError(f"Endpoint {endpoint_id} must be an object")
        _validate_known_keys(f"endpoint {endpoint_id}", endpoint_raw, _ENDPOINT_KEYS)
        api_key = _clean_text(endpoint_raw.get("api_key"))
        if not api_key:
            raise AgentLlmConfigError(f"Endpoint {endpoint_id} api_key is required")
        endpoints[endpoint_id] = {
            "title": _clean_text(endpoint_raw.get("title")) or endpoint_id,
            "api_base": _validate_api_base(endpoint_raw.get("api_base"), field=f"endpoints.{endpoint_id}.api_base"),
            "api_key": api_key,
            "timeout": _float_or_default(endpoint_raw.get("timeout"), settings.console_agent_llm_timeout or 120.0),
            "proxy": _resolve_endpoint_proxy(endpoint_id, endpoint_raw),
        }

    profiles: list[AgentLlmProfile] = []
    for profile_id_raw, profile_raw in profiles_raw.items():
        profile_id = _validate_id("profile", str(profile_id_raw))
        if not isinstance(profile_raw, dict):
            raise AgentLlmConfigError(f"Profile {profile_id} must be an object")
        _validate_known_keys(f"profile {profile_id}", profile_raw, _PROFILE_KEYS)
        endpoint_id = _clean_text(profile_raw.get("endpoint"))
        if not endpoint_id:
            raise AgentLlmConfigError(f"Profile {profile_id} endpoint is required")
        if endpoint_id not in endpoints:
            raise AgentLlmConfigError(f"Profile {profile_id} references unknown endpoint {endpoint_id}")
        model = _clean_text(profile_raw.get("model"))
        if not model:
            raise AgentLlmConfigError(f"Profile {profile_id} model is required")
        endpoint = endpoints[endpoint_id]
        profiles.append(
            AgentLlmProfile(
                id=profile_id,
                title=_clean_text(profile_raw.get("title")) or model,
                mode="config_file",
                endpoint_id=endpoint_id,
                endpoint_title=str(endpoint["title"]),
                model=model,
                api_base=endpoint["api_base"],
                api_key_env=None,
                api_key=str(endpoint["api_key"]),
                timeout=float(endpoint["timeout"]),
                proxy=endpoint["proxy"],
                temperature=_optional_float(profile_raw.get("temperature"))
                if "temperature" in profile_raw
                else float(settings.console_agent_llm_temperature or 0.0),
                reasoning_effort=_optional_text(profile_raw.get("reasoning_effort"))
                if "reasoning_effort" in profile_raw
                else _optional_text(settings.console_agent_reasoning_effort),
                reasoning_summary=_optional_text(profile_raw.get("reasoning_summary"))
                if "reasoning_summary" in profile_raw
                else _optional_text(settings.console_agent_reasoning_summary),
                show_reasoning=_optional_bool(profile_raw.get("show_reasoning"))
                if "show_reasoning" in profile_raw
                else bool(settings.console_agent_show_reasoning),
            )
        )

    default_profile_id = _clean_text(payload.get("default_profile")) or profiles[0].id
    if default_profile_id not in {profile.id for profile in profiles}:
        raise AgentLlmConfigError(f"default_profile {default_profile_id} does not exist")
    return AgentLlmCatalog(
        mode="config_file",
        default_profile_id=default_profile_id,
        profiles=tuple(profiles),
    )


def get_agent_llm_catalog() -> AgentLlmCatalog:
    path = _config_path()
    if path.is_file():
        return _config_file_catalog(path)
    return _single_env_catalog()


def get_public_agent_llm_catalog() -> dict[str, Any]:
    try:
        return get_agent_llm_catalog().public_dict()
    except AgentLlmConfigError as exc:
        return {
            "mode": "error",
            "defaultProfileId": "",
            "profiles": [],
            "error": str(exc),
        }


def get_agent_llm_profile(
    profile_id: str | None = None,
    *,
    require_api_key: bool = False,
) -> AgentLlmProfile:
    catalog = get_agent_llm_catalog()
    requested_id = _clean_text(profile_id) or catalog.default_profile_id
    profiles = catalog.by_id()
    profile = profiles.get(requested_id)
    if profile is None:
        raise AgentLlmProfileNotFound(f"Unknown LLM profile: {requested_id}")

    api_base = profile.api_base
    if require_api_key:
        api_base = _validate_api_base(
            api_base,
            field="CONSOLE_AGENT_LLM_API_BASE" if profile.mode == "single_env" else f"endpoints.{profile.endpoint_id}.api_base",
        )

    api_key = profile.api_key
    if require_api_key and not api_key:
        source = "api_key" if profile.mode == "config_file" else profile.api_key_env or "CONSOLE_AGENT_LLM_API_KEY or OPENAI_API_KEY"
        raise AgentLlmConfigError(f"{source} is required for LLM profile {profile.id}")
    return AgentLlmProfile(
        id=profile.id,
        title=profile.title,
        mode=profile.mode,
        endpoint_id=profile.endpoint_id,
        endpoint_title=profile.endpoint_title,
        model=profile.model,
        api_base=api_base,
        api_key_env=profile.api_key_env,
        api_key=api_key,
        timeout=profile.timeout,
        proxy=profile.proxy,
        temperature=profile.temperature,
        reasoning_effort=profile.reasoning_effort,
        reasoning_summary=profile.reasoning_summary,
        show_reasoning=profile.show_reasoning,
    )


def resolve_agent_llm_profile(profile_id: str | None = None) -> AgentLlmProfile:
    return get_agent_llm_profile(profile_id, require_api_key=True)
