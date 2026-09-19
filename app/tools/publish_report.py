"""The `publish_report` tool: Markdown in, PDF download link out.

Built per conversation (closure over `conversation_id` and an `on_published`
callback) so the server can push a download card to the right WebSocket the
moment the file lands, independent of what the model says afterwards.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import tool

from ..chart_render import FORMS, describe_channels
from ..config import settings
from ..db import repository
from ..pdf import render_report_pdf
from ..storage import StoredReport, storage


def _cover_meta(author: str | None, database: str = "") -> list[tuple[str, str]]:
    """The cover's metadata block, supplied by the server rather than the model.

    Date, requester and source database are facts the process knows; letting the
    model state them would make them assertable, and therefore omittable. The
    pack's build date rides along as a data-currency stamp.
    """
    meta: list[tuple[str, str]] = [("Generated", datetime.now().strftime("%Y-%m-%d"))]
    if author:
        meta.append(("Prepared for", author))
    if database:
        meta.append(("Source", database.upper()))
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


# Raw graphics the model might embed. `markdown_to_body` deliberately passes SVG
# through — that is how a server-drawn chart reaches the PDF — so without this check
# `CLAUDE.md`'s "never write <svg>" is a rule nothing enforces.
#
# A *complete* element, matching what the renderer actually preserves: prose that
# mentions `<svg>` without closing it draws nothing and is not worth refusing over.
_RAW_GRAPHIC = re.compile(r"(?s)<svg\b.*?</svg\s*>|<img\s[^>]*src\s*=\s*[\"']data:", re.I)


def _reject_hand_drawn_graphics(body: str) -> str | None:
    """A refusal if the body embeds a picture the server did not draw, else None.

    A hand-drawn chart is wrong in a way no reader can see: its geometry is not a
    function of the data, so it can look right and be wrong, and a refresh updates
    the numbers beside it while the picture keeps showing last quarter's.
    """
    if not _RAW_GRAPHIC.search(body):
        return None
    return (
        "Rejected: `body_markdown` contains raw <svg> or an embedded image. Charts are "
        "drawn by the server from the rows the analysis recorded — that is what keeps a "
        "figure consistent with its table, on the house palette, and correct after a "
        "refresh.\n\n"
        "Declare the chart on `record_analysis` instead:\n"
        f"  chart_type = one of {', '.join(sorted(FORMS))}\n"
        f"  chart_spec = which result columns fill which channel — {describe_channels()}\n\n"
        "If no form fits, use a Markdown table and say so in the footnote. Remove the "
        "markup and call publish_report again."
    )


def _place_figures(body: str, runs: list[dict[str, Any]]) -> str:
    """Insert each analysis's chart under its heading, captioned by position.

    The model never writes a figure number: it is a property of where the chart
    lands in this report, which is only known here (docs §3.2). A `{{figure:A-…}}`
    placeholder is honoured for a cross-reference; otherwise the chart is appended
    after the matching `##` section heading.
    """
    serial = 0
    for run in runs:
        if not run.get("chart_svg"):
            continue
        serial += 1
        label = f"Figure {serial}"
        block = (f'\n\n<figure class="chart">'
                 f'<figcaption class="chart__title">'
                 f'<span class="chart__id">{label}</span>{html.escape(run["title"])}'
                 f'</figcaption>'
                 + (f'<p class="chart__subtitle">{html.escape(run["subtitle"])}</p>'
                    if run.get("subtitle") else "")
                 + run["chart_svg"] + "</figure>\n\n")

        marker = f"{{{{figure:{run.get('label', '')}}}}}"
        if marker in body:
            body = body.replace(marker, block)
            continue
        # No placeholder: put it under the heading whose text matches the analysis.
        heading = f"## {run['title']}"
        if heading in body:
            idx = body.index(heading) + len(heading)
            body = body[:idx] + block + body[idx:]
        else:
            body += block
    return body


def build_tool(conversation_id: str, on_published: OnPublished | None = None,
               author: str | None = None, database: str = ""):
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
        refusal = _reject_hand_drawn_graphics(body)
        if refusal:
            return _error(refusal)

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
        runs = await repository.runs_for_report(resolved)
        # Figures are placed for rendering only. `body` stays exactly what the model
        # wrote, so the stored record shows its work rather than ours.
        rendered = _place_figures(body, runs)
        try:
            pdf = await asyncio.to_thread(
                render_report_pdf, title, rendered, author, subtitle,
                _cover_meta(author, database))
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
            await repository.record_figures(rec["id"], runs)
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
