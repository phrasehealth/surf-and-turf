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

async def create_conversation(conversation_id: str, user_id: str | None,
                              database: str = "") -> None:
    async with db.begin() as conn:
        await conn.execute(text(
            "INSERT INTO conversations (id, user_id, database)"
            " VALUES (:id, :user_id, :database) ON CONFLICT (id) DO NOTHING"
        ), {"id": conversation_id, "user_id": user_id, "database": database or None})


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
            "SELECT id, serial, user_id, database, sdk_session_id, started_at,"
            "       last_used_at, closed_at, total_cost_usd, total_turns"
            "  FROM conversations WHERE id = :id AND deleted_at IS NULL"
        ), {"id": conversation_id})).mappings().first()
    return dict(row) if row else None


async def list_conversations(user_id: str | None, limit: int = 50) -> list[dict[str, Any]]:
    sql = ("SELECT c.id, c.serial, c.user_id, c.database, c.started_at, c.last_used_at,"
           "       c.total_cost_usd,"
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


# --------------------------------------------------------------------- analyses

async def record_analysis(*, conversation_id: str, title: str, subtitle: str | None,
                          note_template: str | None, chart_type: str | None,
                          chart_spec: dict[str, Any] | None, created_by: str | None,
                          queries: list[dict[str, Any]], parameters: list[dict[str, Any]],
                          relations: list[tuple[str, str]], joins: list[dict[str, Any]],
                          run: dict[str, Any], supersedes_serial: int | None = None,
                          ) -> dict[str, Any]:
    """Write a specification and the run that produced it, in one transaction.

    Correcting an analysis makes a new version of the same lineage rather than
    editing the old one: a published report points at a run, and that run's
    specification has to stay exactly as it was (docs §7).
    """
    async with db.begin() as conn:
        lineage_id, version, supersedes_id = None, 1, None
        if supersedes_serial is not None:
            prior = (await conn.execute(text(
                "SELECT id, lineage_id, version FROM analyses WHERE serial = :s"
            ), {"s": supersedes_serial})).mappings().first()
            if prior is None:
                raise ValueError(f"no analysis with serial {supersedes_serial}")
            lineage_id, supersedes_id = prior["lineage_id"], prior["id"]
            version = (await conn.execute(text(
                "SELECT max(version) + 1 FROM analyses WHERE lineage_id = :l"
            ), {"l": lineage_id})).scalar_one()

        analysis = (await conn.execute(text(
            "INSERT INTO analyses (lineage_id, version, supersedes_id,"
            "                      origin_conversation_id, title, subtitle,"
            "                      note_template, chart_type, chart_spec, created_by)"
            " VALUES (coalesce(cast(:lineage AS uuid), gen_random_uuid()), :version,"
            "         cast(:supersedes AS uuid), :cid, :title, :subtitle, :note,"
            "         :chart_type, cast(:chart_spec AS jsonb), :by)"
            " RETURNING id, serial, version"
        ), {"lineage": str(lineage_id) if lineage_id else None, "version": version,
            "supersedes": str(supersedes_id) if supersedes_id else None,
            "cid": conversation_id, "title": title, "subtitle": subtitle,
            "note": note_template, "chart_type": chart_type,
            "chart_spec": _json(chart_spec) if chart_spec else None,
            "by": created_by})).mappings().one()
        aid = analysis["id"]

        query_ids = []
        for i, q in enumerate(queries, start=1):
            qid = (await conn.execute(text(
                "INSERT INTO analysis_queries (analysis_id, seq, purpose, is_primary,"
                "                              sql_template)"
                " VALUES (:aid, :seq, :purpose, :primary, :sql) RETURNING id"
            ), {"aid": aid, "seq": i, "purpose": q.get("purpose"),
                "primary": bool(q.get("primary")), "sql": q["sql_template"]})).scalar_one()
            query_ids.append(qid)

        if parameters:
            await conn.execute(text(
                "INSERT INTO analysis_parameters (analysis_id, name, kind, operator,"
                "                                 value, expression, label)"
                " VALUES (:aid, :name, :kind, :operator, cast(:value AS jsonb),"
                "         :expression, :label)"
            ), [{"aid": aid, "name": p["name"], "kind": p.get("kind", "other"),
                 "operator": p.get("operator", "in"), "value": _json(p.get("value")),
                 "expression": p.get("expression"), "label": p.get("label")}
                for p in parameters])
        if relations:
            await conn.execute(text(
                "INSERT INTO analysis_relations (analysis_id, schema_name, relation_name)"
                " VALUES (:aid, :schema, :relation) ON CONFLICT DO NOTHING"
            ), [{"aid": aid, "schema": s, "relation": r} for s, r in relations])
        if joins:
            await conn.execute(text(
                "INSERT INTO analysis_joins (analysis_id, left_ref, right_ref, verified,"
                "                            match_pct)"
                " VALUES (:aid, :left, :right, :verified, :pct) ON CONFLICT DO NOTHING"
            ), [{"aid": aid, "left": j["left"], "right": j["right"],
                 "verified": bool(j.get("verified")), "pct": j.get("match_pct")}
                for j in joins])

        run_id = (await conn.execute(text(
            "INSERT INTO analysis_runs (analysis_id, ran_by, origin, bound_params,"
            "                           database_name, role_name, error, freshness,"
            "                           resolved_note, chart_svg, chart_error)"
            " VALUES (:aid, :by, :origin, cast(:params AS jsonb), :db, :role, :error,"
            "         cast(:freshness AS jsonb), :note, :svg, :chart_error)"
            " RETURNING id"
        ), {"aid": aid, "by": created_by, "origin": run.get("origin", "adopted"),
            "params": _json(run.get("bound_params") or {}), "db": run["database_name"],
            "role": run.get("role_name"), "error": run.get("error"),
            "freshness": _json(run["freshness"]) if run.get("freshness") else None,
            "note": run.get("resolved_note"), "svg": run.get("chart_svg"),
            "chart_error": run.get("chart_error")})).scalar_one()

        for qid, q in zip(query_ids, queries):
            await conn.execute(text(
                "INSERT INTO analysis_run_queries (run_id, analysis_query_id, tool_use_id,"
                "        resolved_sql, template_verified, duration_ms, row_count, error,"
                "        result_digest)"
                " VALUES (:run, :q, :tool, :sql, :verified, :ms, :rows, :error, :digest)"
            ), {"run": run_id, "q": qid, "tool": q.get("result_ref"),
                "sql": q.get("resolved_sql") or q["sql_template"],
                "verified": q.get("template_verified"), "ms": q.get("duration_ms"),
                "rows": q.get("row_count"), "error": q.get("error"),
                "digest": q.get("result_digest")})

    return {"id": aid, "serial": analysis["serial"], "version": analysis["version"],
            "run_id": run_id,
            "label": f"A-{analysis['serial']}.{analysis['version']}"}


async def list_analyses(conversation_id: str) -> list[dict[str, Any]]:
    """The cart: what this conversation has recorded, newest last."""
    async with db.begin() as conn:
        rows = (await conn.execute(text(
            "SELECT a.id, a.serial, a.version, a.title, a.subtitle, a.chart_type,"
            "       'A-' || a.serial || '.' || a.version AS label, a.created_at,"
            "       a.note_template,"
            "       (SELECT id FROM analysis_runs r WHERE r.analysis_id = a.id"
            "         ORDER BY ran_at DESC LIMIT 1) AS latest_run_id"
            "  FROM analyses a"
            " WHERE a.origin_conversation_id = :cid AND a.archived_at IS NULL"
            "   AND NOT EXISTS (SELECT 1 FROM analyses newer"
            "                    WHERE newer.lineage_id = a.lineage_id"
            "                      AND newer.version > a.version)"
            " ORDER BY a.created_at"
        ), {"cid": conversation_id})).mappings().all()
    return [dict(r) for r in rows]


async def resolve_analysis_labels(conversation_id: str, labels: list[str],
                                  ) -> tuple[list[dict[str, Any]], list[str]]:
    """(resolved, unknown) for labels like 'A-1042.1', scoped to this conversation."""
    known = {a["label"]: a for a in await list_analyses(conversation_id)}
    resolved, unknown = [], []
    for label in labels:
        hit = known.get(label.strip())
        (resolved if hit else unknown).append(hit or label)
    return resolved, unknown


async def link_report_contents(report_id: Any, analyses: list[dict[str, Any]]) -> None:
    """A report snapshots RUNS, so a later refresh cannot retouch it."""
    rows = [{"r": report_id, "run": a["latest_run_id"], "pos": i}
            for i, a in enumerate(analyses, start=1) if a.get("latest_run_id")]
    if not rows:
        return
    async with db.begin() as conn:
        await conn.execute(text(
            "INSERT INTO report_contents (report_id, analysis_run_id, position)"
            " VALUES (:r, :run, :pos)"
        ), rows)


async def runs_for_report(analyses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The latest run of each chosen analysis, with its chart, in the given order."""
    ids = [a["latest_run_id"] for a in analyses if a.get("latest_run_id")]
    if not ids:
        return []
    async with db.begin() as conn:
        rows = (await conn.execute(text(
            "SELECT r.id AS run_id, r.chart_svg, r.resolved_note, a.title, a.subtitle,"
            "       a.chart_type"
            "  FROM analysis_runs r JOIN analyses a ON a.id = r.analysis_id"
            " WHERE r.id = ANY(:ids)"
        ), {"ids": ids})).mappings().all()
    by_id = {r["run_id"]: dict(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


async def record_figures(report_id: Any, runs: list[dict[str, Any]]) -> int:
    """One row per chart, captioned by its position in this report.

    The SVG is copied rather than referenced: a published PDF must stay true to
    itself even if the analysis is later corrected (docs §3.2).
    """
    rows, serial = [], 0
    for run in runs:
        if not run.get("chart_svg"):
            continue
        serial += 1
        rows.append({"report": report_id, "run": run["run_id"], "serial": serial,
                     "label": f"Figure {serial}", "chart_type": run["chart_type"] or "chart",
                     "title": run["title"], "subtitle": run.get("subtitle"),
                     "note": run.get("resolved_note"), "svg": run["chart_svg"]})
    if not rows:
        return 0
    async with db.begin() as conn:
        await conn.execute(text(
            "INSERT INTO figures (report_id, analysis_run_id, serial, label, chart_type,"
            "                     title, subtitle, note, svg)"
            " VALUES (:report, :run, :serial, :label, :chart_type, :title, :subtitle,"
            "         :note, :svg)"
        ), rows)
    return len(rows)
