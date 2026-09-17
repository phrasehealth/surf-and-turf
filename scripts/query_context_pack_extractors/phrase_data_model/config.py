"""Where this extractor's inputs live, and what it is allowed to spend.

Everything Phrase-specific is in this file. The stages themselves are written
against the spec, not against Epic/Clarity, so pointing them at another dbt
project mostly means editing the constants here.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# --- inputs ---------------------------------------------------------------
# The snowflake-etl checkout. Overridable so CI can point at its own clone.
SNOWFLAKE_ETL_DIR = Path(
    os.getenv("SNOWFLAKE_ETL_DIR", Path.home() / "Documents" / "snowflake-etl")
).expanduser()

# dbt projects inside it. `manifest` is relative to the project dir.
PROJECTS = {
    "epic": {
        "project_dir": "epic/transforms",
        "manifest": "target/manifest.json",
        "vocabulary": "clarity",
    },
    "cerner": {
        "project_dir": "cerner/transforms",
        "manifest": "target/manifest.json",
        "vocabulary": "millennium",
    },
}

PROFILES_YML = "profiles.yml"  # relative to SNOWFLAKE_ETL_DIR

# --- outputs --------------------------------------------------------------
# The pack lands inside the agent's workspace, so it is simply a subdirectory of
# the project the agent already has open: relative paths in the pack's own
# README resolve, and the Dockerfile's `COPY workspace` ships it. Build the pack
# before building the image.
DEFAULT_OUT = REPO_ROOT / "workspace" / "qcp"
# Intermediate facts stay outside the workspace: they are build state, not
# context, and the agent must never read them.
DEFAULT_WORK = REPO_ROOT / "data" / "qcp-work"

# --- introspection --------------------------------------------------------
# Schemas to describe. Empty means "every schema the role can see".
INCLUDE_SCHEMAS: list[str] = []
EXCLUDE_SCHEMAS = ["INFORMATION_SCHEMA"]

# Schemas summarised in index.md. Others still get pages and columns.tsv rows
# (spec §5.3), so they stay greppable; only the standing summary is narrowed.
INDEX_SCHEMAS = ["gold"]

# --- measurement budget ---------------------------------------------------
# Relations larger than this are skipped by scanning stages unless --force.
MAX_SCAN_BYTES = 5 * 1024**3           # 5 GB
STALE_AFTER_DAYS = 30
DATE_TYPES = ("DATE", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ", "DATETIME")

# Column names that look like keys but are ingest plumbing. Never join candidates.
JOIN_DENYLIST = {
    "line", "extract_date", "extract_year", "extract_month", "ingest_id",
    "file_last_modified", "update_date", "updated_at", "created_at", "load_id",
    "batch_id", "_fivetran_synced", "dbt_scd_id", "dbt_updated_at",
}
# A candidate join column must look like a key.
JOIN_KEY_SUFFIXES = ("_id", "_csn_id", "_key", "_num", "_code")
MAX_JOIN_PAIRS = 400                   # hard cap on verification queries
MIN_JOIN_MATCH_PCT = 0.0               # emit even poor matches; the pct is the signal

PROFILE_TOP_N = 10
PROFILE_SAMPLE_ROWS = 5_000_000        # above this, profile from a sample


def project_paths(project: str) -> tuple[Path, Path]:
    """(project_dir, manifest_path) for a configured dbt project."""
    if project not in PROJECTS:
        raise SystemExit(f"unknown project {project!r}; known: {', '.join(PROJECTS)}")
    cfg = PROJECTS[project]
    pdir = SNOWFLAKE_ETL_DIR / cfg["project_dir"]
    return pdir, pdir / cfg["manifest"]
