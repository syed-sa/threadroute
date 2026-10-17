"""Central configuration. Everything is read from environment variables at call time
(so tests and the CLI can change them), with safe defaults that need no API keys."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_FALLBACK_MODELS = "gemini-3.6-flash,gemini-3.5-flash-lite"


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_env() -> None:
    """Load .env from the repo root if python-dotenv is installed."""
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env", override=False)
    except Exception:  # pragma: no cover - dotenv is optional at runtime
        pass


@dataclass(frozen=True)
class Settings:
    home: Path
    live_apis: bool
    model: str
    fallback_models: tuple[str, ...]
    maps_api_key: str | None
    carrier_api_base_url: str | None
    oauth_credentials: str | None
    sheet_id: str | None
    claims_email_to: str
    ops_email: str
    has_gemini_credentials: bool

    @property
    def wms_db(self) -> Path:
        return self.home / "wms.db"

    @property
    def app_db(self) -> Path:
        return self.home / "app.db"

    @property
    def outbox_dir(self) -> Path:
        return self.home / "outbox"

    @property
    def sheet_csv(self) -> Path:
        return self.home / "action_log.csv"

    @property
    def reports_dir(self) -> Path:
        return REPO_ROOT / "reports"


def get_settings() -> Settings:
    home = Path(os.environ.get("THREADROUTE_HOME") or (REPO_ROOT / ".threadroute"))
    home.mkdir(parents=True, exist_ok=True)
    fallbacks = os.environ.get("THREADROUTE_FALLBACK_MODELS", DEFAULT_FALLBACK_MODELS)
    use_vertex = _bool(os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"))
    has_creds = bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")) or (
        use_vertex and bool(os.environ.get("GOOGLE_CLOUD_PROJECT"))
    )
    return Settings(
        home=home,
        live_apis=_bool(os.environ.get("THREADROUTE_LIVE_APIS"), False),
        model=os.environ.get("THREADROUTE_MODEL", DEFAULT_MODEL),
        fallback_models=tuple(m.strip() for m in fallbacks.split(",") if m.strip()),
        maps_api_key=os.environ.get("GOOGLE_MAPS_API_KEY") or None,
        carrier_api_base_url=os.environ.get("CARRIER_API_BASE_URL") or None,
        oauth_credentials=os.environ.get("GOOGLE_OAUTH_CREDENTIALS") or None,
        sheet_id=os.environ.get("GOOGLE_SHEET_ID") or None,
        claims_email_to=os.environ.get("CLAIMS_EMAIL_TO", "claims@carrier-desk.example.com"),
        ops_email=os.environ.get("OPS_EMAIL", "ops@aurelio-menswear.example.com"),
        has_gemini_credentials=has_creds,
    )


def mcp_errlog():
    """Where MCP server subprocess stderr goes: hidden unless THREADROUTE_DEBUG=true."""
    import sys

    if _bool(os.environ.get("THREADROUTE_DEBUG")):
        return sys.stderr
    return open(os.devnull, "w")
