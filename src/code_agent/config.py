from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = ROOT / "config" / "llm.env"

PROVIDER_ALIASES = {
    "auto": "auto",
    "deepseek": "deepseek",
    "openai": "openai",
    "chatgpt": "openai",
    "claude": "claude",
    "anthropic": "claude",
    "qwen": "qwen",
    "dashscope": "qwen",
}

PROVIDER_DEFAULTS = {
    "auto": {
        "base_url": "",
        "model": "auto-cascade",
        "api_key_names": (),
        "base_url_names": (),
        "model_names": ("CODE_AGENT_AUTO_MODEL",),
        "protocol_names": (),
        "protocol": "openai",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_names": ("DEEPSEEK_API_KEY",),
        "base_url_names": ("DEEPSEEK_BASE_URL",),
        "model_names": ("DEEPSEEK_MODEL",),
        "protocol_names": ("DEEPSEEK_PROTOCOL",),
        "protocol": "openai",
    },
    "openai": {
        "base_url": "https://api.openai-proxy.org/v1",
        "model": "gpt-4o",
        "api_key_names": ("CLOSEAI_API_KEY",),
        "base_url_names": ("CLOSEAI_BASE_URL",),
        "model_names": ("CLOSEAI_OPENAI_MODEL", "CLOSEAI_MODEL"),
        "protocol_names": ("CLOSEAI_OPENAI_PROTOCOL", "CLOSEAI_PROTOCOL"),
        "protocol": "openai",
    },
    "claude": {
        "base_url": "https://api.openai-proxy.org/v1",
        "model": "claude-haiku-4-5",
        "api_key_names": ("CLOSEAI_API_KEY",),
        "base_url_names": ("CLOSEAI_BASE_URL",),
        "model_names": ("CLOSEAI_CLAUDE_MODEL", "CLOSEAI_MODEL"),
        "protocol_names": ("CLOSEAI_CLAUDE_PROTOCOL", "CLOSEAI_PROTOCOL"),
        "protocol": "openai",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "api_key_names": ("QWEN_API_KEY", "DASHSCOPE_API_KEY"),
        "base_url_names": ("QWEN_BASE_URL", "DASHSCOPE_BASE_URL"),
        "model_names": ("QWEN_MODEL", "DASHSCOPE_MODEL"),
        "protocol_names": ("QWEN_PROTOCOL", "DASHSCOPE_PROTOCOL"),
        "protocol": "openai",
    },
}

QWEN_AUTO_MODEL_DEFAULTS = {
    "qwen-flash": ("QWEN_FLASH_MODEL", "qwen3.7-flash"),
    "qwen-coder": ("QWEN_CODER_MODEL", "qwen3-coder-next"),
    "qwen-math": ("QWEN_MATH_MODEL", "qwen-math-plus"),
    "qwen-plus": ("QWEN_PLUS_MODEL", "qwen3.7-plus"),
    "qwen-max": ("QWEN_MAX_MODEL", "qwen3.8-max"),
}


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    base_url: str
    model: str
    api_key: str
    api_key_env_names: tuple[str, ...]
    protocol: str
    env_file: Path
    context_tokens: int
    repo_map_chars: int


@dataclass(frozen=True)
class SemanticRouterConfig:
    base_url: str
    model: str
    api_key: str
    timeout_seconds: int


def load_llm_config(
    env_file: str | os.PathLike[str] | None = None,
    provider: str | None = None,
) -> LLMConfig:
    path = Path(env_file or os.getenv("CODE_AGENT_ENV_FILE") or DEFAULT_ENV_FILE).resolve()
    values = _read_env_file(path)

    # Real environment variables override the file, which is useful for CI or one-off runs.
    configured_raw_provider = _first_nonempty("CODE_AGENT_PROVIDER", values) or "deepseek"
    configured_provider = PROVIDER_ALIASES.get(configured_raw_provider.strip().lower())
    if configured_provider is None:
        supported = ", ".join(sorted(PROVIDER_DEFAULTS))
        raise ValueError(
            f"Unknown CODE_AGENT_PROVIDER={configured_raw_provider!r}. Choose one of: {supported}."
        )

    raw_provider = provider or configured_raw_provider
    provider = PROVIDER_ALIASES.get(raw_provider.strip().lower())
    if provider is None:
        supported = ", ".join(sorted(PROVIDER_DEFAULTS))
        raise ValueError(f"Unknown provider={raw_provider!r}. Choose one of: {supported}.")

    # The web dropdown can override the env-selected provider for one request. In that
    # case, do not accidentally reuse a generic DeepSeek value from the local env file.
    use_common_values = (
        provider != "auto"
        and (provider == configured_provider or raw_provider == configured_raw_provider)
    )

    defaults = PROVIDER_DEFAULTS[provider]
    # Provider-specific overrides take precedence over the common values. This prevents
    # an old DeepSeek URL in a local env file from breaking a newly selected relay.
    base_url = (
        _first_nonempty_from_names(tuple(defaults["base_url_names"]), values)
        or (_first_nonempty("CODE_AGENT_BASE_URL", values) if use_common_values else "")
        or defaults["base_url"]
    )
    model = (
        _first_nonempty_from_names(tuple(defaults["model_names"]), values)
        or (_first_nonempty("CODE_AGENT_MODEL", values) if use_common_values else "")
        or defaults["model"]
    )
    api_key_names = tuple(defaults["api_key_names"])
    api_key = _first_nonempty_from_names(api_key_names, values)
    if not api_key and use_common_values:
        api_key = _first_nonempty("CODE_AGENT_API_KEY", values)

    raw_protocol = (
        _first_nonempty_from_names(tuple(defaults["protocol_names"]), values)
        or (_first_nonempty("CODE_AGENT_PROTOCOL", values) if use_common_values else "")
        or str(defaults["protocol"])
    )
    protocol = raw_protocol.strip().lower()
    if protocol not in {"openai", "anthropic"}:
        raise ValueError("CODE_AGENT_PROTOCOL must be 'openai' or 'anthropic'.")

    context_tokens = _read_int("CODE_AGENT_CONTEXT_TOKENS", values, 32000, minimum=4000)
    repo_map_chars = _read_int("CODE_AGENT_REPO_MAP_CHARS", values, 6000, minimum=800)

    return LLMConfig(
        provider=provider,
        base_url=base_url.rstrip("/"),
        model=model,
        api_key=api_key,
        api_key_env_names=api_key_names,
        protocol=protocol,
        env_file=path,
        context_tokens=context_tokens,
        repo_map_chars=repo_map_chars,
    )


def load_qwen_auto_models(
    env_file: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    path = Path(env_file or os.getenv("CODE_AGENT_ENV_FILE") or DEFAULT_ENV_FILE).resolve()
    values = _read_env_file(path)
    return {
        route_name: os.getenv(env_name) or values.get(env_name) or default_model
        for route_name, (env_name, default_model) in QWEN_AUTO_MODEL_DEFAULTS.items()
    }


def load_auto_route_limits(
    env_file: str | os.PathLike[str] | None = None,
) -> tuple[int, int]:
    path = Path(env_file or os.getenv("CODE_AGENT_ENV_FILE") or DEFAULT_ENV_FILE).resolve()
    values = _read_env_file(path)
    legacy_efficient = _read_int("CODE_AGENT_AUTO_QWEN_MAX_SCORE", values, 2, minimum=0)
    legacy_balanced = _read_int("CODE_AGENT_AUTO_DEEPSEEK_MAX_SCORE", values, 5, minimum=0)
    efficient = _read_int(
        "CODE_AGENT_AUTO_EFFICIENT_MAX_SCORE", values, legacy_efficient, minimum=0
    )
    balanced = _read_int(
        "CODE_AGENT_AUTO_BALANCED_MAX_SCORE", values, legacy_balanced, minimum=efficient
    )
    return efficient, balanced


def load_semantic_router_config(
    env_file: str | os.PathLike[str] | None = None,
) -> SemanticRouterConfig:
    path = Path(env_file or os.getenv("CODE_AGENT_ENV_FILE") or DEFAULT_ENV_FILE).resolve()
    values = _read_env_file(path)
    return SemanticRouterConfig(
        base_url=_first_nonempty("CODE_AGENT_ROUTER_BASE_URL", values).rstrip("/"),
        model=(
            _first_nonempty("CODE_AGENT_ROUTER_MODEL", values)
            or "katanemo/Arch-Router-1.5B"
        ),
        api_key=_first_nonempty("CODE_AGENT_ROUTER_API_KEY", values),
        timeout_seconds=_read_int(
            "CODE_AGENT_ROUTER_TIMEOUT_SECONDS", values, 90, minimum=1
        ),
    )


def _read_int(key: str, values: dict[str, str], default: int, minimum: int) -> int:
    raw = os.getenv(key) or values.get(key)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _first_nonempty(key: str, values: dict[str, str]) -> str:
    return os.getenv(key) or values.get(key) or ""


def _first_nonempty_from_names(names: tuple[str, ...], values: dict[str, str]) -> str:
    for name in names:
        value = os.getenv(name) or values.get(name)
        if value:
            return value
    return ""


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values
