"""HTTP + WebSocket front door.

  GET  /                              chat UI
  GET  /healthz                       liveness
  POST /conversations                 -> {conversation_id}
  GET  /conversations/{id}            -> {reports: [...]}
  DELETE /conversations/{id}          close the agent session
  WS   /ws/{id}                       send {"prompt": "..."}; receive event stream
  GET  /reports/{filename}            download (local storage mode only)

Auth: this service trusts an upstream identity-aware proxy (ALB+Cognito, Okta,
oauth2-proxy...) to set `X-Forwarded-User` / `X-Auth-Request-Email`.  The value is
attached to the agent session for attribution and passed to Bedrock as `user`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (Body, FastAPI, HTTPException, Request, WebSocket,
                     WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agent import manager
from .config import ROOT, settings
from .db import db, repository, writer
from .storage import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("report-agent")

STATIC = ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    async def reaper():
        while True:
            await asyncio.sleep(300)
            await manager.reap_idle()

    # Persistence is required: failing here is the point, so a misconfigured
    # deployment stops at boot rather than losing data quietly.
    await db.connect()
    writer.start()
    task = asyncio.create_task(reaper())
    log.info("agent_mode=%s snowflake_mode=%s storage=%s bedrock=%s model=%s",
             settings.agent_mode, settings.snowflake_mode, settings.report_storage,
             settings.use_bedrock, settings.model)
    try:
        yield
    finally:
        task.cancel()
        await manager.close_all()
        await writer.stop()          # flush what is queued before the pool closes
        await db.close()


app = FastAPI(title="Report Agent", lifespan=lifespan)
if settings.cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _user(req_headers) -> str | None:
    return (req_headers.get("x-forwarded-user") or req_headers.get("x-auth-request-email")
            or req_headers.get("x-user-email"))


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC / "index.html").read_text()


_DB_CACHE: dict[str, Any] = {"at": 0.0, "names": []}


async def _selectable_databases() -> list[str]:
    """What the configured role can actually use, cached briefly.

    Mock mode has no catalogue to ask, so it offers a single stand-in rather than
    blocking the offline path.
    """
    if settings.snowflake_mode != "real":
        # The database the mock fixtures live in, so the same guard applies offline.
        from .tools.snowflake_sql import MockBackend

        return sorted({t.split(".")[0] for t in MockBackend.TABLES})
    if time.time() - _DB_CACHE["at"] < 300 and _DB_CACHE["names"]:
        return _DB_CACHE["names"]
    from .tools.snowflake_sql import SnowflakeBackend

    try:
        rows = await SnowflakeBackend().run(
            "SELECT database_name FROM snowflake.information_schema.databases "
            "ORDER BY 1")
        names = [r["DATABASE_NAME"] for r in rows]
    except Exception as exc:
        log.warning("could not list databases: %s", exc)
        return _DB_CACHE["names"]
    _DB_CACHE.update(at=time.time(), names=names)
    return names


@app.get("/c/{cid}")
async def conversation_page(cid: str):
    """The same single page. The path names the conversation so a refresh resumes it."""
    return HTMLResponse((ROOT / "static" / "index.html").read_text())


@app.get("/healthz")
async def healthz():
    # Persistence is required, so a database that is down means the app cannot do
    # its job and should say so rather than accept traffic it will fail to record.
    ok = await db.healthy()
    return JSONResponse(
        {"ok": ok, "database": "up" if ok else "down", "sessions": len(manager.sessions),
         "agent_mode": settings.agent_mode, "events_queued": writer.queue.qsize(),
         "events_written": writer.written, "events_dropped": writer.dropped},
        status_code=200 if ok else 503,
    )


@app.get("/databases")
async def list_databases():
    """Databases a conversation may be started against.

    Temporary: this asks Snowflake what the configured role can see. It becomes a
    property of the signed-in user's session, at which point the choice is made for
    the user rather than offered to them.
    """
    return {"databases": await _selectable_databases()}


@app.post("/conversations")
async def create_conversation(request: Request, body: dict | None = Body(default=None)):
    """Start a conversation against one database.

    The database is required and cannot change afterwards: it is enforced on every
    statement, so it is part of what a conversation *is*, not a setting it carries.
    """
    database = str((body or {}).get("database") or "").strip()
    if not database:
        available = await _selectable_databases()
        raise HTTPException(400, {
            "error": "a database is required to start a conversation",
            "databases": available,
        })
    allowed = await _selectable_databases()
    if allowed and database not in allowed:
        raise HTTPException(400, {
            "error": f"{database!r} is not available to this role",
            "databases": allowed,
        })
    s = manager.create(user_id=_user(request.headers), database=database)
    await repository.create_conversation(s.id, s.user_id, database)
    return {"conversation_id": s.id, "database": database}


@app.get("/conversations")
async def list_conversations(request: Request, mine: bool = True, limit: int = 50):
    """Past conversations, live or not — the index a history view reads."""
    user = _user(request.headers) if mine else None
    return {"conversations": await repository.list_conversations(user, limit)}


@app.get("/conversations/{cid}")
async def get_conversation(cid: str):
    """Answers from the database, so a conversation outlives its in-memory session."""
    row = await repository.get_conversation(cid)
    if row is None:
        raise HTTPException(404, "unknown conversation")
    live = manager.get(cid)
    reports = live.reports if live else await repository.list_reports(cid)
    return {"conversation_id": cid, "serial": row["serial"], "reports": reports,
            "database": row.get("database"), "created_at": row["started_at"],
            "live": live is not None}


@app.get("/conversations/{cid}/transcript")
async def get_transcript(cid: str):
    """Replay: the stored events in the UI's own wire format (docs §5)."""
    if await repository.get_conversation(cid) is None:
        raise HTTPException(404, "unknown conversation")
    return {"conversation_id": cid, "events": await repository.read_transcript(cid)}


@app.delete("/conversations/{cid}")
async def delete_conversation(cid: str):
    await manager.close(cid)
    await repository.close_conversation(cid)
    return {"ok": True}


@app.get("/reports/{filename}")
async def download_report(filename: str):
    path = storage.path_for(filename)
    if path is None:
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="application/pdf", filename=filename)


@app.websocket("/ws/{cid}")
async def ws(websocket: WebSocket, cid: str):
    session = manager.get(cid) or await manager.revive(cid)
    if session is None:
        await websocket.close(code=4404, reason="unknown conversation")
        return
    await websocket.accept()
    await websocket.send_json({"type": "hello", "conversation_id": cid,
                               "agent_mode": settings.agent_mode,
                               "database": session.database,
                               "reports": session.reports})
    try:
        while True:
            data = await websocket.receive_json()
            prompt = (data.get("prompt") or "").strip()
            if not prompt:
                continue
            await websocket.send_json({"type": "turn_start"})
            turn_id = await repository.start_turn(cid, prompt)
            writer.put(cid, turn_id, {"type": "user", "text": prompt})
            result: dict | None = None
            try:
                async for ev in session.send(prompt):
                    await websocket.send_json(ev)
                    writer.put(cid, turn_id, ev)     # never blocks, never raises
                    if ev.get("type") == "result":
                        result = ev
            except Exception as e:  # keep the socket alive; report the failure
                log.exception("turn failed for %s", cid)
                err = {"type": "error", "message": f"{type(e).__name__}: {e}"}
                await websocket.send_json(err)
                writer.put(cid, turn_id, err)
                result = {"is_error": True}
            await repository.end_turn(turn_id, result)
            await websocket.send_json({"type": "turn_end"})
    except WebSocketDisconnect:
        pass
