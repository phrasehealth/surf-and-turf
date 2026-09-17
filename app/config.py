"""Runtime configuration, read once from environment variables.

Every setting has a safe local default so `uvicorn app.main:app` works with
no configuration at all (mock agent, mock Snowflake, local report storage).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# `.env` is read here rather than only by docker compose's `env_file`, so that a
# bare `uvicorn app.main:app` picks up the same settings.  Real environment
# variables win, which keeps `AGENT_MODE=mock uvicorn ...` working.
load_dotenv(ROOT / ".env", override=False)


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _path(name: str) -> str:
    """A file path setting, expanded and resolved against the repo root.

    Lets `.env` say `secrets/snowflake_key.p8` without depending on the
    working directory; absolute values (the container mount) pass through.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    return str(path if path.is_absolute() else ROOT / path)


@dataclass(frozen=True)
class Settings:
    # --- agent -------------------------------------------------------------
    # "sdk"  -> real Claude Agent SDK (needs Bedrock or Anthropic credentials)
    # "mock" -> scripted fake agent so the UI/API can be exercised offline
    agent_mode: str = os.getenv("AGENT_MODE", "mock")
    # Bedrock cross-region inference profile ID, e.g. us.anthropic.claude-sonnet-4-5
    model: str | None = os.getenv("ANTHROPIC_MODEL") or None
    use_bedrock: bool = _bool("CLAUDE_CODE_USE_BEDROCK", False)
    aws_region: str = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
    max_turns: int = int(os.getenv("AGENT_MAX_TURNS", "40"))
    max_budget_usd: float | None = (
        float(os.environ["AGENT_MAX_BUDGET_USD"]) if os.getenv("AGENT_MAX_BUDGET_USD") else None
    )
    # Directory the agent sees as its project: CLAUDE.md, README, transforms/
    workspace_dir: Path = Path(os.getenv("WORKSPACE_DIR", ROOT / "workspace"))

    # --- snowflake ---------------------------------------------------------
    snowflake_mode: str = os.getenv("SNOWFLAKE_MODE", "mock")  # "mock" | "real"
    snowflake_account: str = os.getenv("SNOWFLAKE_ACCOUNT", "")
    snowflake_user: str = os.getenv("SNOWFLAKE_USER", "")
    snowflake_password: str = os.getenv("SNOWFLAKE_PASSWORD", "")
    snowflake_private_key_path: str = _path("SNOWFLAKE_PRIVATE_KEY_PATH")
    snowflake_role: str = os.getenv("SNOWFLAKE_ROLE", "REPORT_READER")
    snowflake_warehouse: str = os.getenv("SNOWFLAKE_WAREHOUSE", "")
    snowflake_database: str = os.getenv("SNOWFLAKE_DATABASE", "")
    snowflake_schema: str = os.getenv("SNOWFLAKE_SCHEMA", "")
    sql_row_limit: int = int(os.getenv("SQL_ROW_LIMIT", "500"))
    sql_timeout_s: int = int(os.getenv("SQL_TIMEOUT_S", "120"))

    # --- reports -----------------------------------------------------------
    report_storage: str = os.getenv("REPORT_STORAGE", "local")  # "local" | "s3"
    report_dir: Path = Path(os.getenv("REPORT_DIR", ROOT / "data" / "reports"))
    s3_bucket: str = os.getenv("REPORT_S3_BUCKET", "")
    s3_prefix: str = os.getenv("REPORT_S3_PREFIX", "reports/")
    presign_ttl_s: int = int(os.getenv("REPORT_URL_TTL_S", str(24 * 3600)))
    # Public base URL of this service (used to build local download links)
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    # Printed shell. The handling marking sits in @bottom-center on every page,
    # cover included; these reports aggregate patient data, so it defaults on.
    # Set REPORT_MARKING="" to drop it for an externally-shared document.
    report_marking: str = os.getenv("REPORT_MARKING", "Internal use only")
    report_page_size: str = os.getenv("REPORT_PAGE_SIZE", "Letter")

    # --- server ------------------------------------------------------------
    session_idle_ttl_s: int = int(os.getenv("SESSION_IDLE_TTL_S", "3600"))
    cors_origins: list[str] = field(
        default_factory=lambda: [o for o in os.getenv("CORS_ORIGINS", "").split(",") if o]
    )


settings = Settings()
