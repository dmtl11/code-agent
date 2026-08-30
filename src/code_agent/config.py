from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = ROOT / "config" / "llm.env"


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str
    env_file: Path


def load_llm_config(env_file: str | os.PathLike[str] | None = None) -> LLMConfig:
    path = Path(env_file or os.getenv("CODE_AGENT_ENV_FILE") or DEFAULT_ENV_FILE).resolve()
    values = _read_env_file(path)

    # Real environment variables override the file, which is useful for CI or one-off runs.
    base_url = os.getenv("CODE_AGENT_BASE_URL") or values.get("CODE_AGENT_BASE_URL") or "https://api.openai.com/v1"
    model = os.getenv("CODE_AGENT_MODEL") or values.get("CODE_AGENT_MODEL") or "gpt-4o-mini"
    api_key = (
        os.getenv("CODE_AGENT_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or values.get("CODE_AGENT_API_KEY")
        or values.get("OPENAI_API_KEY")
        or values.get("DEEPSEEK_API_KEY")
        or ""
    )

    return LLMConfig(base_url=base_url.rstrip("/"), model=model, api_key=api_key, env_file=path)


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

