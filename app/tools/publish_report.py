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


def build_tool(conversation_id: str, on_published: OnPublished | None = None, author: str | None = None):
    @tool(
        "publish_report",
        "Render the finished report to PDF and return a download link. Call this once "
        "the analysis is complete and the user has confirmed the read-back. `title` and "
        "`subtitle` go on the generated cover page — do not repeat either in the body. "
        "`body_markdown` is the report itself: one section per analysis, then the "
        "appendix of queries. The cover's date, requester and source database are added "
        "by the server; do not write them yourself.",
        {"title": str, "body_markdown": str, "subtitle": str},
    )
    async def publish_report(args: dict[str, Any]) -> dict[str, Any]:
        title = (args.get("title") or "Report").strip()
        subtitle = (args.get("subtitle") or "").strip()
        body = args.get("body_markdown") or ""
        if len(body.strip()) < 20:
            return {"content": [{"type": "text", "text": "Rejected: body_markdown is empty."}],
                    "is_error": True}
        try:
            pdf = await asyncio.to_thread(
                render_report_pdf, title, body, author, subtitle, _cover_meta(author))
            stored = await storage.save(title, pdf, conversation_id)
        except Exception as e:
            return {"content": [{"type": "text", "text": f"PDF render/upload failed: {e}"}],
                    "is_error": True}
        try:
            await repository.record_report(
                conversation_id, title=title, subtitle=subtitle,
                storage_backend=settings.report_storage, storage_key=stored.filename,
                report_uid=stored.report_id, size_bytes=stored.size_bytes,
                published_by=author, handling_marking=settings.report_marking or None,
                qcp_built_at=_pack_built_at(), body_markdown=body)
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
