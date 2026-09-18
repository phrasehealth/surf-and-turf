"""Background event writer.

`AgentSession.send()` must not await Postgres. A write failure there would break a
live conversation over data only wanted afterwards, so `send()` enqueues and returns
and a single task drains the queue (docs/persistence-schema.md §7).

The shape is copied from the SDK's own `SessionStore` contract, which solves the same
problem: append after the fact, batch, retry, log, let the conversation continue. A
hard crash loses the tail of the last turn; the alternative is a database outage
taking the product down.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from . import repository as repo

log = logging.getLogger("report-agent.db.writer")

BATCH_MAX = 100
BATCH_WAIT_S = 0.2
QUEUE_MAX = 10_000          # bounded: a stuck writer must not exhaust memory
RETRIES = 3


@dataclass
class _Item:
    conversation_id: str
    turn_id: UUID | None
    event: dict[str, Any]


@dataclass
class EventWriter:
    # Created in start(), not here: an asyncio.Queue binds to the first loop that
    # touches it, and a module-level singleton would otherwise carry a dead loop
    # from one app lifespan into the next.
    queue: asyncio.Queue[_Item | None] | None = None
    maxsize: int = QUEUE_MAX
    _task: asyncio.Task | None = None
    _seq: dict[Any, int] = field(default_factory=dict)
    dropped: int = 0
    written: int = 0

    def start(self) -> None:
        if self._task is not None:
            return
        self.queue = asyncio.Queue(self.maxsize)
        self._seq.clear()
        self._task = asyncio.create_task(self._drain(), name="event-writer")

    async def stop(self) -> None:
        if self._task is None or self.queue is None:
            return
        await self.queue.put(None)              # sentinel: flush what is queued, then exit
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        finally:
            self._task = None
            self.queue = None

    def put(self, conversation_id: str, turn_id: UUID | None, event: dict[str, Any]) -> None:
        """Never blocks and never raises: persistence must not affect the turn."""
        if event.get("type") in repo.SKIP_EVENT_TYPES or self.queue is None:
            return
        try:
            self.queue.put_nowait(_Item(conversation_id, turn_id, event))
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped % 100 == 1:
                log.warning("event queue full; dropped %d event(s)", self.dropped)

    async def _collect(self) -> tuple[list[_Item], bool]:
        """One batch, plus whether the stop sentinel was seen."""
        assert self.queue is not None
        item = await self.queue.get()
        if item is None:
            return [], True
        batch, stopping = [item], False
        while len(batch) < BATCH_MAX:
            try:
                nxt = await asyncio.wait_for(self.queue.get(), timeout=BATCH_WAIT_S)
            except asyncio.TimeoutError:
                break
            if nxt is None:
                stopping = True
                break
            batch.append(nxt)
        return batch, stopping

    async def _drain(self) -> None:
        while True:
            try:
                batch, stopping = await self._collect()
            except asyncio.CancelledError:
                return
            if batch:
                await self._write(batch)
            if stopping:
                return

    async def _write(self, batch: list[_Item]) -> None:
        # Group by (conversation, turn) so each INSERT carries a contiguous seq range.
        groups: dict[tuple[str, Any], list[dict[str, Any]]] = {}
        for it in batch:
            groups.setdefault((it.conversation_id, it.turn_id), []).append(it.event)
        for (cid, tid), events in groups.items():
            start = self._seq.get((cid, tid), 0)
            for attempt in range(1, RETRIES + 1):
                try:
                    n = await repo.append_events(cid, tid, start, events)
                    self._seq[(cid, tid)] = start + n
                    self.written += n
                    break
                except Exception as exc:
                    if attempt == RETRIES:
                        log.error("dropping %d event(s) for %s after %d attempts: %s",
                                  len(events), cid[:8], RETRIES, exc)
                    else:
                        await asyncio.sleep(0.2 * attempt)

    def forget(self, conversation_id: str) -> None:
        for key in [k for k in self._seq if k[0] == conversation_id]:
            self._seq.pop(key, None)


writer = EventWriter()
