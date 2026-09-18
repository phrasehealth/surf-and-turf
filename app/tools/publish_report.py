"""The `publish_report` tool: Markdown in, PDF download link out.

Built per conversation (closure over `conversation_id` and an `on_published`
callback) so the server can push a download card to the right WebSocket the
moment the file lands, independent of what the model says afterwards.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import tool

from ..config import settings
from ..db import repository
from ..pdf import render_report_pdf
from ..storage import StoredReport, storage


def _cover_meta(author: str | None) -> list[tuple[str, str]]:
    """The cover's metadata block, supplied by the server rather than the model.

    Date, requester and source database are facts the process knows; letting the
    model state them would make them assertable, and therefore omittable. The
    pack's build date rides along as a data-currency stamp.
    """
    meta: list[tuple[str, str]] = [("Generated", datetime.now().strftime("%Y-%m-%d"))]
    if author:
        meta.append(("Prepared for", author))
    if settings.snowflake_database:
        meta.append(("Source", settings.snowflake_database.upper()))
    built = _pack_built_at()
    if built:
        meta.append(("Schema as of", built.date().isoformat()))
    return meta


def _pack_built_at() -> datetime | None:
    """When the Query Context Pack behind these numbers was built.

    Returned as a datetime, not a string: asyncpg binds by Python type and will
    not coerce text into a timestamptz the way psycopg does.
    """
    manifest = Path(settings.workspace_dir) / "qcp" / "MANIFEST.md"
    try:
        for line in manifest.read_text().splitlines():
            if line.startswith("built_at:"):
                raw = line.split(":", 1)[1].strip()
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (OSError, ValueError):
        pass
    return None

OnPublished = Callable[[StoredReport, str], Awaitable[None]]

log = logging.getLogger("report-agent.tools")

_RECORD_FIRST = (
    "Rejected: `analyses` is empty, but the body has {sections} section(s). Every "
    "analysis in a report must be recorded before it can be published.\n\n"
    "For each one, call:\n"
    "  record_analysis(title, subtitle, note_template,\n"
    "                  queries=[{{result_ref, sql_template, purpose, primary}}],\n"
    "                  parameters, relations, joins, chart_type, chart_spec)\n\n"
    "Pass the result_ref each run_sql returned, so nothing is "
    "re-run. Each call returns a label like 'A-1042.1'. Then call publish_report "
    "again with those labels in `analyses`, in the order they appear in the body."
)


def _error(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def build_tool(conversation_id: str, on_published: OnPublished | None = None, author: str | None = None):
    @tool(
        "publish_report",
        "Render the finished report to PDF and return a download link. Call this once "
        "the analysis is complete and the user has confirmed the read-back. `title` and "
        "`subtitle` go on the generated cover page — do not repeat either in the body. "
        "`body_markdown` is the report itself: one section per analysis, then the "
        "appendix of queries. `analyses` is the ordered list of labels from "
        "`record_analysis` — every analysis in the body must have been recorded first. "
        "The cover's date, requester and source database are added by the server; do "
        "not write them yourself.",
        {"title": str, "body_markdown": str, "subtitle": str, "analyses": list},
    )
    async def publish_report(args: dict[str, Any]) -> dict[str, Any]:
        title = (args.get("title") or "Report").strip()
        subtitle = (args.get("subtitle") or "").strip()
        body = args.get("body_markdown") or ""
        if len(body.strip()) < 20:
            return {"content": [{"type": "text", "text": "Rejected: body_markdown is empty."}],
                    "is_error": True}

        # Every analysis in a report must be recorded first, so the numbers can be
        # traced, refreshed and reused (docs/persistence-schema.md §7). The refusal
        # is written to be acted on, not merely to be correct.
        labels = [str(x) for x in (args.get("analyses") or [])]
        resolved, unknown = await repository.resolve_analysis_labels(conversation_id, labels)
        sections = body.count("\n## ") + (1 if body.startswith("## ") else 0)
        if not labels:
            return _error(_RECORD_FIRST.format(sections=sections or "several"))
        if unknown:
            return _error(
                f"Rejected: {', '.join(unknown)} " +
                ("is not an analysis" if len(unknown) == 1 else "are not analyses") +
                " recorded in this conversation. Use the labels `record_analysis` "
                "returned, exactly as given.")
        if sections and len(resolved) != sections:
            # A heuristic: a report may legitimately carry a section that is not an
            # analysis, so this warns rather than refusing.
            log.warning("publish: %d analyses for %d body sections in %s",
                        len(resolved), sections, conversation_id[:8])
        try:
            pdf = await asyncio.to_thread(
                render_report_pdf, title, body, author, subtitle, _cover_meta(author))
            stored = await storage.save(title, pdf, conversation_id)
        except Exception as e:
            return {"content": [{"type": "text", "text": f"PDF render/upload failed: {e}"}],
                    "is_error": True}
        try:
            rec = await repository.record_report(
                conversation_id, title=title, subtitle=subtitle,
                storage_backend=settings.report_storage, storage_key=stored.filename,
                report_uid=stored.report_id, size_bytes=stored.size_bytes,
                published_by=author, handling_marking=settings.report_marking or None,
                qcp_built_at=_pack_built_at(), body_markdown=body)
            await repository.link_report_contents(rec["id"], resolved)
        except Exception:
            # The PDF exists and the user should get it; a bookkeeping failure is
            # logged, not raised back at the model as a publish failure.
            logging.getLogger("report-agent.tools").exception(
                "failed to record report %s", stored.report_id)
        if on_published:
            await on_published(stored, title)
        payload = {"status": "published", "title": title, "download_url": stored.url,
                   "filename": stored.filename, "size_bytes": stored.size_bytes}
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}

    return publish_report
