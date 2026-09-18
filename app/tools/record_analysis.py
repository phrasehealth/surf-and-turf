"""The `record_analysis` tool: an analysis as a reusable specification.

Recording is independent of publishing (docs/persistence-schema.md §7). Many
conversations end with the user satisfied and no PDF; those analyses are still worth
keeping, and they are what a later report is assembled from.

The tool **adopts** rather than executes: each query names the `result_ref` that a
`run_sql` handed back, and the server binds the specification to results it already
holds. A reference rather than the tool_use_id because a model never sees the id of
its own call — it can only quote what came back in a result. Nothing is re-run, and the numbers in the report are the numbers that
were computed.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from claude_agent_sdk import tool

from ..config import settings
from ..db import repository
from .result_cache import ResultCache, bind_template, matches

log = logging.getLogger("report-agent.tools")

DESCRIPTION = (
    "Record one analysis — a title, the queries behind it, its filters and its chart — "
    "so it can be published, refreshed later against new data, or reused in another "
    "report. Call this as each analysis is finished, before reading back to the user; "
    "recording does not publish anything. Pass the `result_ref` each `run_sql` "
    "you already made so nothing is re-run. Returns a label like 'A-1042.1' to cite in "
    "`publish_report`."
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Analysis title, as it appears in the report."},
        "subtitle": {"type": "string", "description": "One line on what the reader is looking at."},
        "note_template": {"type": "string",
                          "description": "The footnote: date range, source relations, caveats."},
        "queries": {
            "type": "array",
            "description": "The queries behind this analysis, in order. Exactly one primary.",
            "items": {
                "type": "object",
                "properties": {
                    "result_ref": {"type": "string",
                                   "description": "The `result_ref` that run_sql returned "
                                                  "for this query, e.g. 'q1'."},
                    "sql_template": {"type": "string",
                                     "description": "The SQL with :named parameters in place of "
                                                    "swappable filter values."},
                    "purpose": {"type": "string", "description": "e.g. 'cohort', 'series'."},
                    "primary": {"type": "boolean",
                                "description": "True for the query the table and chart come from."},
                },
                "required": ["result_ref", "sql_template"],
            },
        },
        "parameters": {
            "type": "array",
            "description": "Swappable filters. A filter is a parameter when it is a set "
                           "(IN) or a range (BETWEEN, <, >). A plain equality usually is not.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The :name used in sql_template."},
                    "kind": {"type": "string",
                             "description": "date_range | diagnosis | medication | orderset | "
                                            "procedure | alert | panel | flowsheet_row | other"},
                    "operator": {"type": "string",
                                 "enum": ["in", "between", "gt", "gte", "lt", "lte", "eq"]},
                    "value": {"description": "The bound value: a list for `in`, "
                                             "[from, to] for `between`, else a scalar."},
                    "expression": {"type": "string",
                                   "description": "What the user asked for, in their words."},
                    "label": {"type": "string", "description": "UI label, e.g. 'Patient population'."},
                },
                "required": ["name", "kind", "operator", "value"],
            },
        },
        "relations": {"type": "array", "items": {"type": "string"},
                      "description": "Relations read, as 'schema.relation'."},
        "joins": {
            "type": "array",
            "description": "Joins used. `verified` is false when qcp/joins.tsv had no row.",
            "items": {
                "type": "object",
                "properties": {
                    "left": {"type": "string"}, "right": {"type": "string"},
                    "verified": {"type": "boolean"},
                    "match_pct": {"type": "number"},
                },
                "required": ["left", "right", "verified"],
            },
        },
        "chart_type": {"type": "string",
                       "description": "hbar | vbar | stacked | lines | stat_tiles, or omit "
                                      "for a table-only analysis."},
        "chart_spec": {"type": "object",
                       "description": "Which result columns map to which channel."},
        "supersedes": {"type": "string",
                       "description": "An earlier label, e.g. 'A-1042.1', when correcting it. "
                                      "Makes a new version rather than a separate analysis."},
    },
    "required": ["title", "queries"],
}


def build_tool(conversation_id: str, cache: ResultCache, author: str | None = None,
               freshness: Callable[[], dict[str, Any]] | None = None):
    @tool("record_analysis", DESCRIPTION, SCHEMA)
    async def record_analysis(args: dict[str, Any]) -> dict[str, Any]:
        queries = args.get("queries") or []
        if not queries:
            return _error("Rejected: `queries` is empty. An analysis needs at least one "
                          "query — pass the result_ref and sql_template of the run_sql "
                          "call that produced its numbers.")
        if sum(1 for q in queries if q.get("primary")) > 1:
            return _error("Rejected: more than one query is marked `primary`. Exactly one "
                          "is the query the table and chart are drawn from.")
        if not any(q.get("primary") for q in queries):
            queries[0]["primary"] = True      # a single-query analysis needs no ceremony

        params = {p["name"]: p.get("value") for p in (args.get("parameters") or [])}
        adopted, missing = [], []
        for q in queries:
            # By the reference run_sql handed back; then by the SQL itself, bound or
            # raw, so a query written without parameters still resolves.
            entry = (cache.get_by_ref(q.get("result_ref", ""))
                     or cache.get_by_sql(bind_template(q["sql_template"], params))
                     or cache.get_by_sql(q["sql_template"]))
            if entry is None:
                missing.append(q.get("result_ref") or "(no result_ref given)")
                continue
            adopted.append({
                **q,
                "resolved_sql": entry.sql,
                "row_count": entry.row_count,
                "duration_ms": entry.duration_ms,
                "error": entry.error,
                # Templates may approximate (§7): recorded, never rejected.
                "template_verified": matches(q["sql_template"], params, entry.sql),
            })
        if missing:
            # Say what *is* adoptable: guessing at an identifier is the failure mode
            # this replaced, and an error that only says "no" invites more guessing.
            available = cache.recent()
            listing = ("\n".join(f"  {e.ref}  {' '.join(e.sql.split())[:90]}"
                                  for e in available)
                       if available else "  (nothing — run the query first)")
            return _error(
                f"Rejected: no result for {', '.join(missing)}.\n\n"
                "`record_analysis` adopts a query you already ran. Every `run_sql` reply "
                "includes a `result_ref` like 'q1' — pass that.\n\n"
                f"Available now:\n{listing}")

        supersedes_serial = _serial_of(args.get("supersedes"))
        try:
            rec = await repository.record_analysis(
                conversation_id=conversation_id,
                title=args["title"].strip(), subtitle=(args.get("subtitle") or "").strip() or None,
                note_template=args.get("note_template"), chart_type=args.get("chart_type"),
                chart_spec=args.get("chart_spec"), created_by=author,
                queries=adopted, parameters=args.get("parameters") or [],
                relations=[_split(r) for r in (args.get("relations") or [])],
                joins=args.get("joins") or [],
                run={"origin": "adopted", "bound_params": params,
                     "database_name": settings.snowflake_database or "unknown",
                     "role_name": settings.snowflake_role or None,
                     "freshness": freshness() if freshness else None,
                     "resolved_note": args.get("note_template")},
                supersedes_serial=supersedes_serial)
        except ValueError as e:
            return _error(f"Rejected: {e}")
        except Exception as e:
            log.exception("record_analysis failed")
            return _error(f"Could not record the analysis: {type(e).__name__}: {e}")

        unverified = [q for q in adopted if not q["template_verified"]]
        payload: dict[str, Any] = {"status": "recorded", "label": rec["label"],
                                   "queries": len(adopted)}
        if unverified:
            # Not a failure — the template only has to be close (§7) — but the agent
            # should know, because a refresh will run the template, not what ran today.
            payload["note"] = (f"{len(unverified)} template(s) did not reproduce the SQL "
                               f"exactly; a refresh will run the template.")
        return _text(payload)

    return record_analysis


def _split(ref: str) -> tuple[str, str]:
    parts = str(ref).split(".")
    return (parts[-2], parts[-1]) if len(parts) >= 2 else ("", parts[-1])


def _serial_of(label: str | None) -> int | None:
    """'A-1042.1' -> 1042. The version is implied: a correction supersedes the latest."""
    if not label:
        return None
    try:
        return int(str(label).strip().lstrip("Aa-").split(".")[0])
    except ValueError:
        return None


def _text(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


def _error(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}
