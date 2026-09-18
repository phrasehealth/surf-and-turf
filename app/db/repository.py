"""Reads and writes. SQLAlchemy Core: this schema is written against, not navigated.

Only what has a writer today lives here — conversations, turns, events, reports.
The analysis tables exist in the migration but nothing populates them until the
`record_analysis` tool does (docs/persistence-schema.md §9).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from .engine import db

log = logging.getLogger("report-agent.db")

# Events that are transport control or streaming noise: a replayer synthesises
# `turn_start`/`turn_end`/`hello` from `turns`, and `assistant_text` supersedes the
# `text_delta` fragments it is assembled from (docs §5, "What not to store").
SKIP_EVENT_TYPES = {"text_delta", "turn_start", "turn_end", "hello", "preset"}


# --------------------------------------------------------------- conversations

async def create_conversation(conversation_id: str, user_id: str | None) -> None:
    async with db.begin() as conn:
        await conn.execute(text(
            "INSERT INTO conversations (id, user_id) VALUES (:id, :user_id)"
            " ON CONFLICT (id) DO NOTHING"
        ), {"id": conversation_id, "user_id": user_id})


async def touch_conversation(conversation_id: str) -> None:
    async with db.begin() as conn:
        await conn.execute(text(
            "UPDATE conversations SET last_used_at = now() WHERE id = :id"
        ), {"id": conversation_id})


async def close_conversation(conversation_id: str) -> None:
    async with db.begin() as conn:
        await conn.execute(text(
            "UPDATE conversations SET closed_at = now() WHERE id = :id AND closed_at IS NULL"
        ), {"id": conversation_id})


async def get_conversation(conversation_id: str) -> dict[str, Any] | None:
    async with db.begin() as conn:
        row = (await conn.execute(text(
            "SELECT id, serial, user_id, sdk_session_id, started_at, last_used_at,"
            "       closed_at, total_cost_usd, total_turns"
            "  FROM conversations WHERE id = :id AND deleted_at IS NULL"
        ), {"id": conversation_id})).mappings().first()
    return dict(row) if row else None


async def list_conversations(user_id: str | None, limit: int = 50) -> list[dict[str, Any]]:
    sql = ("SELECT c.id, c.serial, c.user_id, c.started_at, c.last_used_at, c.total_cost_usd,"
           "       (SELECT count(*) FROM turns t WHERE t.conversation_id = c.id) AS turns,"
           "       (SELECT count(*) FROM reports r WHERE r.origin_conversation_id = c.id)"
           "         AS reports"
           "  FROM conversations c WHERE c.deleted_at IS NULL")
    params: dict[str, Any] = {"limit": limit}
    if user_id:
        sql += " AND c.user_id = :user_id"
        params["user_id"] = user_id
    sql += " ORDER BY c.last_used_at DESC LIMIT :limit"
    async with db.begin() as conn:
        rows = (await conn.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


# ----------------------------------------------------------- turns and events

async def start_turn(conversation_id: str, prompt: str) -> UUID:
    async with db.begin() as conn:
        seq = (await conn.execute(text(
            "SELECT coalesce(max(seq), 0) + 1 FROM turns WHERE conversation_id = :cid"
        ), {"cid": conversation_id})).scalar_one()
        return (await conn.execute(text(
            "INSERT INTO turns (conversation_id, seq, prompt) VALUES (:cid, :seq, :prompt)"
            " RETURNING id"
        ), {"cid": conversation_id, "seq": seq, "prompt": prompt})).scalar_one()


async def end_turn(turn_id: UUID, result: dict[str, Any] | None) -> None:
    result = result or {}
    async with db.begin() as conn:
        await conn.execute(text(
            "UPDATE turns SET ended_at = now(), duration_ms = :ms,"
            "       cost_usd_running = :cost, is_error = :err WHERE id = :id"
        ), {"id": turn_id, "ms": result.get("duration_ms"),
            "cost": result.get("cost_usd"), "err": bool(result.get("is_error"))})
        # The SDK reports conversation totals, so the latest turn carries the total.
        if result.get("cost_usd") is not None or result.get("session_id"):
            await conn.execute(text(
                "UPDATE conversations c SET total_cost_usd = coalesce(:cost, c.total_cost_usd),"
                "       total_turns = coalesce(:turns, c.total_turns),"
                "       sdk_session_id = coalesce(:sid, c.sdk_session_id)"
                "  FROM turns t WHERE t.id = :tid AND c.id = t.conversation_id"
            ), {"tid": turn_id, "cost": result.get("cost_usd"),
                "turns": result.get("num_turns"), "sid": result.get("session_id")})


async def append_events(conversation_id: str, turn_id: UUID | None,
                        start_seq: int, events: list[dict[str, Any]]) -> int:
    """Append a batch. Returns how many rows were written."""
    rows = []
    seq = start_seq
    for ev in events:
        if ev.get("type") in SKIP_EVENT_TYPES:
            continue
        rows.append({"cid": conversation_id, "tid": turn_id, "seq": seq,
                     "type": ev["type"],
                     "tool_use_id": ev.get("id") or ev.get("tool_use_id"),
                     "payload": ev})
        seq += 1
    if not rows:
        return 0
    async with db.begin() as conn:
        await conn.execute(text(
            "INSERT INTO events (conversation_id, turn_id, seq, type, tool_use_id, payload)"
            " VALUES (:cid, :tid, :seq, :type, :tool_use_id,"
            "         cast(:payload AS jsonb))"
        ), [{**r, "payload": _json(r["payload"])} for r in rows])
    return len(rows)


def _json(v: Any) -> str:
    import json
    return json.dumps(v, default=str)


async def read_transcript(conversation_id: str) -> list[dict[str, Any]]:
    """Replay: the stored events, in order, as the UI's own wire format."""
    async with db.begin() as conn:
        rows = (await conn.execute(text(
            "SELECT e.type, e.payload, e.at, t.seq AS turn_seq, t.prompt"
            "  FROM events e LEFT JOIN turns t ON t.id = e.turn_id"
            " WHERE e.conversation_id = :cid ORDER BY e.id"
        ), {"cid": conversation_id})).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------- reports

async def record_report(conversation_id: str, *, title: str, subtitle: str,
                        storage_backend: str, storage_key: str, report_uid: str,
                        size_bytes: int, published_by: str | None,
                        handling_marking: str | None, qcp_built_at: datetime | None,
                        body_markdown: str | None) -> dict[str, Any]:
    async with db.begin() as conn:
        row = (await conn.execute(text(
            "INSERT INTO reports (origin_conversation_id, title, subtitle, storage_backend,"
            "                     storage_key, report_uid, size_bytes, published_by,"
            "                     handling_marking, qcp_built_at, body_markdown)"
            " VALUES (:cid, :title, :subtitle, :backend, :key, :uid, :size, :by,"
            "         :marking, :qcp, :body)"
            " RETURNING id, serial"
        ), {"cid": conversation_id, "title": title, "subtitle": subtitle,
            "backend": storage_backend, "key": storage_key, "uid": report_uid,
            "size": size_bytes, "by": published_by, "marking": handling_marking,
            "qcp": qcp_built_at, "body": body_markdown})).mappings().one()
    return dict(row)


async def list_reports(conversation_id: str) -> list[dict[str, Any]]:
    async with db.begin() as conn:
        rows = (await conn.execute(text(
            "SELECT serial, title, subtitle, storage_key, report_uid, size_bytes,"
            "       published_at FROM reports WHERE origin_conversation_id = :cid"
            " ORDER BY published_at"
        ), {"cid": conversation_id})).mappings().all()
    return [dict(r) for r in rows]
