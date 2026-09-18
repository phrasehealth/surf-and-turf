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
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
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


@app.post("/conversations")
async def create_conversation(request: Request):
    s = manager.create(user_id=_user(request.headers))
    await repository.create_conversation(s.id, s.user_id)
    return {"conversation_id": s.id}


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
            "created_at": row["started_at"], "live": live is not None}


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
                               "agent_mode": settings.agent_mode, "reports": session.reports})
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
