"""The workspace boundary, tested without a model or a warehouse.

Both directions matter. A guard that is too loose leaks the Snowflake key; one
that is too tight cuts the agent off from the Query Context Pack, which fails
quietly -- it falls back to list_tables and writes worse SQL rather than erroring.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.workspace_guard import build_hook, check

WS = Path("/srv/report-agent/workspace")


@pytest.mark.parametrize("tool,args", [
    ("Read", {"file_path": "qcp/relations/gold/alert_events.md"}),          # relative
    ("Read", {"file_path": "/srv/report-agent/workspace/qcp/columns.tsv"}),  # absolute
    ("Read", {"file_path": "CLAUDE.md"}),
    ("Glob", {"pattern": "qcp/concepts/*.md"}),
    ("Glob", {"pattern": "**/*.md", "path": "qcp"}),
    ("Grep", {"pattern": "pat_enc_csn_id", "path": "qcp"}),
    ("Grep", {"pattern": "D66"}),                                            # no path at all
])
def test_workspace_paths_are_allowed(tool, args):
    allowed, reason = check(tool, args, WS)
    assert allowed, f"{tool}{args} should be allowed but was denied: {reason}"


@pytest.mark.parametrize("tool,args", [
    ("Read", {"file_path": "/run/secrets/snowflake_key.p8"}),         # the warehouse key
    ("Read", {"file_path": "/home/agent/.aws/credentials"}),          # Bedrock creds
    ("Read", {"file_path": "../secrets/snowflake_key_2.p8"}),         # escape by ..
    ("Read", {"file_path": "qcp/../../.env"}),                        # escape mid-path
    ("Read", {"file_path": "/etc/passwd"}),
    ("Glob", {"pattern": "/run/secrets/**"}),                         # absolute glob
    ("Grep", {"pattern": "AKIA", "path": "/home/agent/.aws"}),        # grep elsewhere
])
def test_paths_outside_the_workspace_are_denied(tool, args):
    allowed, reason = check(tool, args, WS)
    assert not allowed, f"{tool}{args} should have been denied"
    assert reason


def test_tools_without_paths_are_not_the_guards_business():
    assert check("mcp__reporting__run_sql", {"sql": "SELECT 1"}, WS)[0]
    assert check("publish_report", {"title": "x"}, WS)[0]


def test_hook_returns_a_deny_decision_the_cli_understands():
    hook = build_hook(WS, "conv-1234")
    out = asyncio.run(hook(
        {"tool_name": "Read", "tool_input": {"file_path": "/run/secrets/snowflake_key.p8"}},
        "toolu_1", None))
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "deny"
    assert "workspace" in spec["permissionDecisionReason"]


def test_hook_stays_silent_on_allowed_paths():
    """An empty result means 'no opinion' -- normal permission rules still apply."""
    hook = build_hook(WS, "conv-1234")
    out = asyncio.run(hook(
        {"tool_name": "Read", "tool_input": {"file_path": "qcp/index.md"}}, "toolu_2", None))
    assert out == {}


def test_the_real_pack_is_reachable(tmp_path):
    """Guard against a boundary so tight the agent loses the pack it depends on."""
    from app.config import settings
    ws = Path(settings.workspace_dir)
    for f in ("CLAUDE.md", "qcp/index.md", "qcp/columns.tsv", "qcp/README.md"):
        if (ws / f).exists():
            assert check("Read", {"file_path": f}, ws)[0], f"{f} must stay readable"
