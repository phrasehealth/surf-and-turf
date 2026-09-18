"""Conversation sessions: one Claude Agent SDK client per browser conversation.

`SessionManager` owns the lifecycle.  `AgentSession.send()` turns a user prompt
into an async stream of small JSON-able events the UI understands:

  text_delta        streamed assistant text
  assistant_text    the complete text of one assistant message (authoritative)
  tool_use          the agent called a tool (name + compact input summary)
  tool_result       a tool returned (ok / error + short preview)
  report            a PDF was published (title, url, filename)
  result            turn finished (duration for the turn; cost is the SDK's
                    running total for the conversation, not this turn)
  error             something broke

`MockAgentSession` implements the same interface with scripted output so the
UI and API can be developed and tested without any model access.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .config import settings
from .storage import StoredReport
from .workspace_guard import build_hook
from .tools import publish_report as publish_tool
from .tools import snowflake_sql

log = logging.getLogger("report-agent.agent")

Event = dict[str, Any]
MCP_SERVER_NAME = "reporting"

SYSTEM_APPEND = """
You are a reporting analyst assistant. You have read-only access to Snowflake via
the `run_sql`, `list_tables` and `describe_table` tools, and you can read the
project workspace with Read/Glob/Grep.

The workspace holds a Query Context Pack at `qcp/`: it describes every relation in
the database, its columns and types, what they mean, where they came from and how
current they are. `qcp/README.md` states the lookup protocol; follow it rather than
guessing table or column names. `qcp/index.md` is loaded for you already.

Workflow: clarify the ask if needed -> find the relation in the pack -> query
(aggregate in SQL) -> summarize findings in the chat -> read back what the user asked
for and wait for confirmation -> call `publish_report` with the complete report as
Markdown. After publishing, tell the user the report is ready; the download link is
shown to them automatically.

A report holds one or more analyses and accumulates like a shopping cart: a further
request adds an analysis rather than replacing the report. Never publish without the
read-back. `CLAUDE.md` gives the required structure of an analysis and its footnote.
"""


def _summarize_input(name: str, inp: dict[str, Any]) -> str:
    if name.endswith("run_sql"):
        return " ".join(str(inp.get("sql", "")).split())[:300]
    if name.endswith("publish_report"):
        return f"title={inp.get('title')!r}, {len(str(inp.get('body_markdown', '')))} chars"
    if name in {"Read", "Glob", "Grep"}:
        return str(inp.get("file_path") or inp.get("pattern") or "")[:200]
    return json.dumps(inp, default=str)[:300]


def _preview(content: Any, limit: int = 240) -> str:
    if isinstance(content, list):
        content = " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    s = str(content or "")
    return s if len(s) <= limit else s[:limit] + "…"


# ---------------------------------------------------------------------------
# Real SDK-backed session
# ---------------------------------------------------------------------------


class AgentSession:
    def __init__(self, conversation_id: str, user_id: str | None = None):
        self.id = conversation_id
        self.user_id = user_id
        self.created_at = time.time()
        self.last_used = time.time()
        self.reports: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._client = None
        self._pending_reports: asyncio.Queue[Event] = asyncio.Queue()

    # -- lifecycle ----------------------------------------------------------

    async def _on_published(self, stored: StoredReport, title: str) -> None:
        ev = {"type": "report", "title": title, "url": stored.url,
              "filename": stored.filename, "size_bytes": stored.size_bytes,
              "report_id": stored.report_id, "created_at": stored.created_at}
        self.reports.append(ev)
        await self._pending_reports.put(ev)

    def _build_options(self):
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, create_sdk_mcp_server

        tools = snowflake_sql.build_tools() + [
            publish_tool.build_tool(self.id, self._on_published, author=self.user_id)
        ]
        server = create_sdk_mcp_server(MCP_SERVER_NAME, version="1.0.0", tools=tools)
        mcp_tool_names = [f"mcp__{MCP_SERVER_NAME}__{t.name}" for t in tools]
        builtin = ["Read", "Glob", "Grep"]
        guard_tools = builtin + ["NotebookRead"]  # guard tools we do not enable, too

        env: dict[str, str] = {}
        if settings.use_bedrock:
            env["CLAUDE_CODE_USE_BEDROCK"] = "1"
            env["AWS_REGION"] = settings.aws_region

        return ClaudeAgentOptions(
            cwd=str(settings.workspace_dir),
            setting_sources=["project"],  # loads workspace/CLAUDE.md
            system_prompt={"type": "preset", "preset": "claude_code", "append": SYSTEM_APPEND},
            tools=builtin,
            allowed_tools=builtin + mcp_tool_names,
            mcp_servers={MCP_SERVER_NAME: server},
            permission_mode="dontAsk",  # anything not pre-approved is denied, never prompted
            # `allowed_tools` gates tool names, not paths, and it also shadows
            # `can_use_tool`. A PreToolUse hook is the only layer that sees every
            # file call, so the workspace boundary is enforced there.
            hooks={"PreToolUse": [HookMatcher(
                matcher="|".join(guard_tools),
                hooks=[build_hook(settings.workspace_dir, self.id)],
            )]},
            model=settings.model,
            max_turns=settings.max_turns,
            max_budget_usd=settings.max_budget_usd,
            include_partial_messages=True,
            env=env,  # note: ClaudeAgentOptions.user is an OS user for the subprocess, not an identity tag
            stderr=lambda line: log.debug("[claude %s] %s", self.id[:8], line.rstrip()),
        )

    async def start(self) -> None:
        from claude_agent_sdk import ClaudeSDKClient

        self._client = ClaudeSDKClient(options=self._build_options())
        await self._client.connect()

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            finally:
                self._client = None

    # -- one turn -----------------------------------------------------------

    async def send(self, prompt: str) -> AsyncIterator[Event]:
        from claude_agent_sdk import (
            AssistantMessage, ResultMessage, StreamEvent, TextBlock,
            ToolResultBlock, ToolUseBlock, UserMessage,
        )

        if self._client is None:
            await self.start()
        async with self._lock:
            self.last_used = time.time()
            await self._client.query(prompt)
            async for msg in self._client.receive_response():
                # Drain any reports published by the tool since the last message.
                while not self._pending_reports.empty():
                    yield self._pending_reports.get_nowait()

                if isinstance(msg, StreamEvent):
                    ev = msg.event
                    if ev.get("type") == "content_block_delta":
                        delta = ev.get("delta", {})
                        if delta.get("type") == "text_delta" and msg.parent_tool_use_id is None:
                            yield {"type": "text_delta", "text": delta["text"]}
                elif isinstance(msg, AssistantMessage):
                    if msg.parent_tool_use_id is not None:
                        continue  # subagent chatter
                    text = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
                    if text:
                        yield {"type": "assistant_text", "text": text, "message_id": msg.message_id}
                    for b in msg.content:
                        if isinstance(b, ToolUseBlock):
                            yield {"type": "tool_use", "id": b.id, "name": b.name,
                                   "summary": _summarize_input(b.name, b.input)}
                elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                    for b in msg.content:
                        if isinstance(b, ToolResultBlock):
                            yield {"type": "tool_result", "tool_use_id": b.tool_use_id,
                                   "is_error": bool(b.is_error), "preview": _preview(b.content)}
                elif isinstance(msg, ResultMessage):
                    while not self._pending_reports.empty():
                        yield self._pending_reports.get_nowait()
                    yield {"type": "result", "is_error": msg.is_error, "subtype": msg.subtype,
                           "cost_usd": msg.total_cost_usd, "duration_ms": msg.duration_ms,
                           "num_turns": msg.num_turns, "session_id": msg.session_id}


# ---------------------------------------------------------------------------
# Mock session (no model). Exercises the real tools end to end.
# ---------------------------------------------------------------------------


class MockAgentSession(AgentSession):
    async def start(self) -> None:
        self._client = object()  # sentinel: "started"
        self._sql_tools = {t.name: t for t in snowflake_sql.build_tools()}
        self._publish = publish_tool.build_tool(self.id, self._on_published, author=self.user_id)

    async def close(self) -> None:
        self._client = None

    async def _stream_text(self, text: str) -> AsyncIterator[Event]:
        for word in text.split(" "):
            yield {"type": "text_delta", "text": word + " "}
            await asyncio.sleep(0.015)
        yield {"type": "assistant_text", "text": text, "message_id": uuid.uuid4().hex}

    async def send(self, prompt: str) -> AsyncIterator[Event]:
        if self._client is None:
            await self.start()
        async with self._lock:
            t0 = time.time()
            self.last_used = t0
            wants_pdf = any(k in prompt.lower() for k in ("pdf", "publish", "report", "generate"))

            async for ev in self._stream_text(
                "Mock agent here (set AGENT_MODE=sdk for the real thing). "
                "Let me look at the alert data first."
            ):
                yield ev

            tid = "toolu_" + uuid.uuid4().hex[:8]
            sql = ("SELECT day, alert_name, firings, accept_rate "
                   "FROM ANALYTICS.CDS.ALERT_DAILY ORDER BY day")
            yield {"type": "tool_use", "id": tid, "name": f"mcp__{MCP_SERVER_NAME}__run_sql",
                   "summary": sql}
            res = await self._sql_tools["run_sql"].handler({"sql": sql})
            rows = json.loads(res["content"][0]["text"])["rows"]
            yield {"type": "tool_result", "tool_use_id": tid, "is_error": False,
                   "preview": _preview(res["content"])}

            table = "| Day | Alert | Firings | Accept rate |\n|---|---|---:|---:|\n" + "\n".join(
                f"| {r['DAY']} | {r['ALERT_NAME']} | {r['FIRINGS']} | {r['ACCEPT_RATE']:.1%} |"
                for r in rows
            )
            async for ev in self._stream_text(
                f"Over the last {len(rows)} days the Sepsis Screen alert fired "
                f"{sum(r['FIRINGS'] for r in rows)} times with acceptance rising from "
                f"{rows[0]['ACCEPT_RATE']:.1%} to {rows[-1]['ACCEPT_RATE']:.1%}."
                + (" Publishing the PDF now." if wants_pdf
                   else " Say 'generate the report' and I'll publish a PDF.")
            ):
                yield ev

            if wants_pdf:
                pid = "toolu_" + uuid.uuid4().hex[:8]
                body = ("## Summary\n\nAlert acceptance improved steadily across the week.\n\n"
                        "## Daily detail\n\n" + table + "\n\n## Method\n\nSource: "
                        "`ANALYTICS.CDS.ALERT_DAILY` (mock data).")
                yield {"type": "tool_use", "id": pid, "name": f"mcp__{MCP_SERVER_NAME}__publish_report",
                       "summary": _summarize_input("publish_report", {"title": "Sepsis Screen Weekly", "body_markdown": body})}
                res = await self._publish.handler({"title": "Sepsis Screen Weekly", "body_markdown": body})
                yield {"type": "tool_result", "tool_use_id": pid,
                       "is_error": bool(res.get("is_error")), "preview": _preview(res["content"])}
                while not self._pending_reports.empty():
                    yield self._pending_reports.get_nowait()
                async for ev in self._stream_text("Your report is ready — use the download card above."):
                    yield ev

            yield {"type": "result", "is_error": False, "subtype": "success", "cost_usd": 0.0,
                   "duration_ms": int((time.time() - t0) * 1000), "num_turns": 1, "session_id": self.id}


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


@dataclass
class SessionManager:
    sessions: dict[str, AgentSession] = field(default_factory=dict)

    def create(self, user_id: str | None = None) -> AgentSession:
        cid = str(uuid.uuid4())
        cls = AgentSession if settings.agent_mode == "sdk" else MockAgentSession
        s = cls(cid, user_id=user_id)
        self.sessions[cid] = s
        return s

    def get(self, cid: str) -> AgentSession | None:
        return self.sessions.get(cid)

    async def close(self, cid: str) -> None:
        s = self.sessions.pop(cid, None)
        if s:
            await s.close()

    async def reap_idle(self) -> None:
        cutoff = time.time() - settings.session_idle_ttl_s
        for cid in [c for c, s in self.sessions.items() if s.last_used < cutoff]:
            log.info("closing idle session %s", cid)
            await self.close(cid)

    async def close_all(self) -> None:
        for cid in list(self.sessions):
            await self.close(cid)


manager = SessionManager()
