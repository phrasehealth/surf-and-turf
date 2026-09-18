"""SessionStore adapter: what makes `resume` survive a restart.

The Claude Code subprocess writes transcripts under CLAUDE_CONFIG_DIR, which is
ephemeral in a container — a task replacement destroys every session and `resume`
finds nothing. `ClaudeAgentOptions.session_store` mirrors each transcript line to an
external store, and `resume` materialises from it when the local file is absent.

Only `append()` and `load()` are required, and the SDK duck-types: no subclassing.
`append` is called after the local write has already succeeded, batches at ~100ms,
and is retried three times by the SDK before being surfaced as a MirrorErrorMessage,
so a failure here cannot take down a live conversation.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from .engine import db

log = logging.getLogger("report-agent.db.session_store")


def _key_parts(key: Any) -> tuple[str, str]:
    """SessionKey is (project_key, session_id) in either object or tuple form."""
    session_id = getattr(key, "session_id", None)
    project_key = getattr(key, "project_key", None)
    if session_id is None and isinstance(key, (tuple, list)) and len(key) >= 2:
        project_key, session_id = key[0], key[1]
    return str(project_key or ""), str(session_id or "")


class PostgresSessionStore:
    """Mirrors SDK transcripts so a conversation can be resumed after a restart."""

    async def append(self, key: Any, entries: list[dict[str, Any]]) -> None:
        project_key, session_id = _key_parts(key)
        if not session_id or not entries:
            return
        rows = [{
            "session_id": session_id,
            "project_key": project_key,
            # Entries carrying a uuid are deduplicated; those without (titles, tags,
            # mode markers) are appended as-is, per the SDK's contract.
            "entry_uuid": e.get("uuid"),
            "entry": json.dumps(e, default=str),
        } for e in entries]
        async with db.begin() as conn:
            await conn.execute(text(
                "INSERT INTO sdk_transcript_entries (session_id, project_key, entry_uuid, entry)"
                " VALUES (:session_id, :project_key, cast(:entry_uuid AS uuid),"
                "         cast(:entry AS jsonb))"
                " ON CONFLICT (session_id, entry_uuid) WHERE entry_uuid IS NOT NULL"
                " DO NOTHING"
            ), rows)

    async def load(self, key: Any) -> list[dict[str, Any]] | None:
        """Return every entry for a session, in write order, or None if unknown.

        Deep equality is enough: the SDK never byte-compares, so jsonb key
        reordering is harmless.
        """
        _, session_id = _key_parts(key)
        if not session_id:
            return None
        async with db.begin() as conn:
            rows = (await conn.execute(text(
                "SELECT entry FROM sdk_transcript_entries"
                " WHERE session_id = :sid ORDER BY seq"
            ), {"sid": session_id})).scalars().all()
        if not rows:
            return None
        log.info("resuming session %s from %d stored transcript entries",
                 session_id[:8], len(rows))
        return list(rows)

    async def delete(self, key: Any) -> None:
        _, session_id = _key_parts(key)
        async with db.begin() as conn:
            await conn.execute(text(
                "DELETE FROM sdk_transcript_entries WHERE session_id = :sid"
            ), {"sid": session_id})


session_store = PostgresSessionStore()
