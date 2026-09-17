"""The `publish_report` tool: Markdown in, PDF download link out.

Built per conversation (closure over `conversation_id` and an `on_published`
callback) so the server can push a download card to the right WebSocket the
moment the file lands, independent of what the model says afterwards.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from claude_agent_sdk import tool

from ..pdf import render_report_pdf
from ..storage import StoredReport, storage

OnPublished = Callable[[StoredReport, str], Awaitable[None]]


def build_tool(conversation_id: str, on_published: OnPublished | None = None, author: str | None = None):
    @tool(
        "publish_report",
        "Render the finished report to PDF and return a download link. Call this once "
        "the analysis is complete and the user has confirmed the content. `body_markdown` "
        "is the full report in Markdown (headings, paragraphs, tables). Do not include "
        "the title in the body; pass it separately.",
        {"title": str, "body_markdown": str},
    )
    async def publish_report(args: dict[str, Any]) -> dict[str, Any]:
        title = (args.get("title") or "Report").strip()
        body = args.get("body_markdown") or ""
        if len(body.strip()) < 20:
            return {"content": [{"type": "text", "text": "Rejected: body_markdown is empty."}],
                    "is_error": True}
        try:
            pdf = await asyncio.to_thread(render_report_pdf, title, body, author)
            stored = await storage.save(title, pdf, conversation_id)
        except Exception as e:
            return {"content": [{"type": "text", "text": f"PDF render/upload failed: {e}"}],
                    "is_error": True}
        if on_published:
            await on_published(stored, title)
        payload = {"status": "published", "title": title, "download_url": stored.url,
                   "filename": stored.filename, "size_bytes": stored.size_bytes}
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}

    return publish_report
