"""Environment resolution. Demo by default; production only when asked for explicitly."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REST_BASE = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}
WS_URL = {
    "prod": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    "demo": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
}
WS_SIGN_PATH = "/trade-api/ws/v2"


class ConfigError(RuntimeError):
    """Raised when required settings are missing or invalid."""


@dataclass(frozen=True)
class KalshiEnv:
    name: str  # "demo" or "prod"
    rest_base: str
    ws_url: str
    api_key_id: str
    private_key_path: Path


def resolve_env_name(environ: dict[str, str] | None = None) -> str:
    """Return "demo" unless KALSHI_ENV is exactly "prod". Anything else is an error.

    Never falls back to prod silently.
    """
    environ = os.environ if environ is None else environ
    raw = environ.get("KALSHI_ENV", "").strip().lower()
    if raw in ("", "demo"):
        return "demo"
    if raw == "prod":
        return "prod"
    raise ConfigError(f"KALSHI_ENV must be 'demo' or 'prod', got {raw!r}")


def load_env(environ: dict[str, str] | None = None) -> KalshiEnv:
    environ = os.environ if environ is None else environ
    name = resolve_env_name(environ)
    key_id = environ.get("KALSHI_API_KEY_ID", "").strip()
    key_path = environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
    if not key_id:
        raise ConfigError("KALSHI_API_KEY_ID is not set")
    if not key_path:
        raise ConfigError("KALSHI_PRIVATE_KEY_PATH is not set")
    path = Path(key_path).expanduser()
    if not path.is_file():
        raise ConfigError(f"Private key file not found: {path}")
    return KalshiEnv(
        name=name,
        rest_base=REST_BASE[name],
        ws_url=WS_URL[name],
        api_key_id=key_id,
        private_key_path=path,
    )


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
