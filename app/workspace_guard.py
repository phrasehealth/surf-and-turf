"""Confine the agent's file tools to the workspace directory.

`allowed_tools` gates tool *names*, and `cwd` only decides where relative paths
resolve — neither is a path boundary. Without this, `Read` can reach anything the
container user can: the Snowflake private key at /run/secrets, the AWS credentials
under ~/.aws.

That matters because the agent's context is not all trusted input. `run_sql`
returns free-text clinician content (alert override comments and the like)
straight into the model's context, and a report can carry anything the model read
back out as a PDF. Input surface, target and exit all exist, so the boundary is
worth enforcing rather than documenting.

Implemented as a `PreToolUse` hook because that is the only layer that sees *every*
call: `can_use_tool` is skipped for tools already in `allowed_tools`, which is
exactly how Read/Glob/Grep are configured here.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("report-agent.guard")

# The file tools. MCP tools take no paths, so they are not the hook's business.
GUARDED_TOOLS = ("Read", "Glob", "Grep", "NotebookRead")

# Which argument of each tool names a filesystem location. Grep's `pattern` is a
# regex and Glob's is a glob, so neither is a path on its own -- but a glob may
# carry an absolute prefix, which is checked separately below.
PATH_ARGS = ("file_path", "path", "notebook_path")


def _candidate_paths(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    paths = [str(tool_input[k]) for k in PATH_ARGS if tool_input.get(k)]
    if tool_name == "Glob":
        pattern = str(tool_input.get("pattern") or "")
        # "/etc/**" is a path; "**/*.sql" is not.
        if pattern.startswith("/") or pattern.startswith("~"):
            paths.append(pattern)
    return paths


def _resolve(raw: str, workspace: Path) -> Path:
    """Absolute, symlink- and `..`-collapsed. Non-existent paths resolve fine."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = workspace / p
    return p.resolve()


def check(tool_name: str, tool_input: dict[str, Any], workspace: Path) -> tuple[bool, str]:
    """(allowed, reason). Pure: no SDK, no I/O beyond path resolution."""
    if tool_name not in GUARDED_TOOLS:
        return True, ""
    root = Path(workspace).resolve()
    for raw in _candidate_paths(tool_name, tool_input):
        # A glob pattern's wildcards must not be resolved as literal path parts.
        probe = raw.split("*", 1)[0] if "*" in raw else raw
        if not probe.strip("/"):
            return False, f"{raw!r} is outside the workspace"
        target = _resolve(probe, root)
        if target != root and not target.is_relative_to(root):
            return False, (f"{raw!r} resolves to {target}, outside the workspace "
                           f"({root})")
    return True, ""


def build_hook(workspace: Path, conversation_id: str = ""):
    """A PreToolUse callback that denies file access outside `workspace`."""

    async def guard(input: dict[str, Any], tool_use_id: str | None, context: Any) -> dict:
        tool_name = input.get("tool_name", "")
        allowed, reason = check(tool_name, input.get("tool_input") or {}, workspace)
        if allowed:
            return {}  # no opinion; normal permission rules still apply
        log.warning("[guard %s] denied %s: %s", conversation_id[:8], tool_name, reason)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Denied: {reason}. This agent may only read files under its "
                    f"workspace. Use the Query Context Pack under qcp/, or the "
                    f"list_tables / describe_table tools."
                ),
            }
        }

    return guard
