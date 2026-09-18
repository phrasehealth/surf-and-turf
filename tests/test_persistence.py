"""Persistence: conversations, turns, the event log, reports, and the SDK transcript.

These exercise the real migration against a real Postgres — the schema's constraints
are most of its value, and an in-memory fake would not enforce them.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.db import repository as repo
from app.db.session_store import PostgresSessionStore
from app.db.writer import EventWriter


@pytest.fixture
def run(db_loop):
    """Run a coroutine on the test's loop — see the `db_loop` fixture for why."""
    return db_loop.run_until_complete


def test_conversation_round_trips(db_conn, run):
    cid = str(uuid.uuid4())
    run(repo.create_conversation(cid, "david.do@phrasehealth.com"))
    row = run(repo.get_conversation(cid))
    assert row["id"] == uuid.UUID(cid)
    assert row["user_id"] == "david.do@phrasehealth.com"
    assert row["serial"] >= 1, "serial comes from the identity column"
    assert row["closed_at"] is None

    run(repo.close_conversation(cid))
    assert run(repo.get_conversation(cid))["closed_at"] is not None


def test_creating_a_conversation_twice_is_idempotent(db_conn, run):
    cid = str(uuid.uuid4())
    run(repo.create_conversation(cid, "a@b.c"))
    run(repo.create_conversation(cid, "someone-else@b.c"))
    assert run(repo.get_conversation(cid))["user_id"] == "a@b.c"


def test_turn_and_transcript_round_trip(db_conn, run):
    cid = str(uuid.uuid4())
    run(repo.create_conversation(cid, "a@b.c"))
    turn = run(repo.start_turn(cid, "how many D66 patients?"))

    events = [
        {"type": "user", "text": "how many D66 patients?"},
        {"type": "text_delta", "text": "ignored"},            # streaming noise
        {"type": "tool_use", "id": "toolu_1", "name": "run_sql", "summary": "SELECT 1"},
        {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": False,
         "preview": '{"row_count": 1}'},
        {"type": "assistant_text", "text": "There are 42."},
        {"type": "turn_end"},                                  # transport control
    ]
    written = run(repo.append_events(cid, turn, 0, events))
    assert written == 4, "text_delta and turn_end are not stored"

    run(repo.end_turn(turn, {"duration_ms": 1234, "cost_usd": 0.42,
                             "num_turns": 1, "session_id": "sdk-abc"}))

    transcript = run(repo.read_transcript(cid))
    assert [e["type"] for e in transcript] == [
        "user", "tool_use", "tool_result", "assistant_text"]
    assert transcript[0]["prompt"] == "how many D66 patients?"
    assert transcript[2]["payload"]["preview"] == '{"row_count": 1}'

    convo = run(repo.get_conversation(cid))
    assert float(convo["total_cost_usd"]) == 0.42
    assert convo["sdk_session_id"] == "sdk-abc", "resume needs this after a restart"


def test_report_is_recorded_and_listed(db_conn, run):
    cid = str(uuid.uuid4())
    run(repo.create_conversation(cid, "a@b.c"))
    rec = run(repo.record_report(
        cid, title="Stroke order set utilization", subtitle="Monthly trends",
        storage_backend="local", storage_key="20260918-1200-stroke-abc.pdf",
        report_uid="abc123def456", size_bytes=51234, published_by="a@b.c",
        handling_marking="Internal use only", qcp_built_at=None,
        body_markdown="## Analysis 1"))
    assert rec["serial"] >= 1

    reports = run(repo.list_reports(cid))
    assert len(reports) == 1 and reports[0]["report_uid"] == "abc123def456"


def test_report_uid_is_unique(db_conn, run):
    cid = str(uuid.uuid4())
    run(repo.create_conversation(cid, "a@b.c"))
    kw = dict(title="t", subtitle="", storage_backend="local", storage_key="k.pdf",
              report_uid="dup", size_bytes=1, published_by=None,
              handling_marking=None, qcp_built_at=None, body_markdown=None)
    run(repo.record_report(cid, **kw))
    with pytest.raises(Exception):
        run(repo.record_report(cid, **kw))


def test_a_conversation_cannot_be_deleted_out_from_under_its_reports(db_conn, run):
    """Deleting a conversation must not orphan a PDF that still exists."""
    cid = str(uuid.uuid4())
    run(repo.create_conversation(cid, "a@b.c"))
    run(repo.record_report(cid, title="t", subtitle="", storage_backend="local",
                           storage_key="k.pdf", report_uid=uuid.uuid4().hex,
                           size_bytes=1, published_by=None, handling_marking=None,
                           qcp_built_at=None, body_markdown=None))

    async def _delete():
        async with db_conn.begin() as conn:
            await conn.execute(text("DELETE FROM conversations WHERE id = :id"), {"id": cid})

    with pytest.raises(Exception):
        run(_delete())


def test_list_conversations_filters_by_user(db_conn, run):
    mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())
    run(repo.create_conversation(mine, "me@x.com"))
    run(repo.create_conversation(theirs, "them@x.com"))
    rows = run(repo.list_conversations("me@x.com"))
    assert [str(r["id"]) for r in rows] == [mine]
    assert rows[0]["turns"] == 0 and rows[0]["reports"] == 0


# --------------------------------------------------------------- event writer

def test_writer_never_raises_when_the_queue_is_full(db_loop):
    """Persistence must not be able to break a live turn."""
    async def _go():
        w = EventWriter(maxsize=2)
        w.start()
        w._task.cancel()                      # stop the drain so the queue fills
        for _ in range(10):
            w.put("cid", None, {"type": "assistant_text", "text": "x"})
        return w
    w = db_loop.run_until_complete(_go())
    assert w.dropped == 8 and w.queue.qsize() == 2


def test_writer_drops_streaming_noise_before_queueing(db_loop):
    async def _go():
        w = EventWriter()
        w.start()
        w._task.cancel()
        w.put("cid", None, {"type": "text_delta", "text": "a"})
        w.put("cid", None, {"type": "assistant_text", "text": "a"})
        return w
    w = db_loop.run_until_complete(_go())
    assert w.queue.qsize() == 1


def test_writer_ignores_events_before_it_is_started():
    """A put() before start() must not raise; the app lifespan owns the queue."""
    w = EventWriter()
    w.put("cid", None, {"type": "assistant_text", "text": "x"})
    assert w.queue is None and w.dropped == 0


def test_writer_persists_a_batch_in_order(db_conn, run):
    cid = str(uuid.uuid4())
    run(repo.create_conversation(cid, "a@b.c"))
    turn = run(repo.start_turn(cid, "p"))

    async def _go():
        w = EventWriter()
        w.start()
        for i in range(5):
            w.put(cid, turn, {"type": "assistant_text", "text": f"m{i}"})
        await w.stop()                       # flushes what is queued
        return w

    w = run(_go())
    assert w.written == 5 and w.dropped == 0
    assert [e["payload"]["text"] for e in run(repo.read_transcript(cid))] == \
        ["m0", "m1", "m2", "m3", "m4"]


# ------------------------------------------------------- SDK transcript store

def test_session_store_round_trips_and_deduplicates(db_conn, run):
    store = PostgresSessionStore()
    key = type("K", (), {"project_key": "proj", "session_id": "sess-1"})()
    entries = [{"uuid": str(uuid.uuid4()), "type": "user", "text": "hi"},
               {"uuid": str(uuid.uuid4()), "type": "assistant", "text": "hello"},
               {"type": "title", "value": "no uuid, appended as-is"}]

    run(store.append(key, entries))
    run(store.append(key, entries[:2]))      # the SDK may resend; uuid is the key

    loaded = run(store.load(key))
    assert len(loaded) == 3, "uuid-bearing entries deduplicate, others append"
    assert loaded[0]["text"] == "hi"
    assert run(store.load(type("K", (), {"project_key": "p", "session_id": "nope"})())) is None


def test_session_store_load_preserves_write_order(db_conn, run):
    store = PostgresSessionStore()
    key = type("K", (), {"project_key": "p", "session_id": "ordered"})()
    for i in range(6):
        run(store.append(key, [{"uuid": str(uuid.uuid4()), "n": i}]))
    assert [e["n"] for e in run(store.load(key))] == [0, 1, 2, 3, 4, 5]


# ------------------------------------------------------ analysis parameters

def test_parameter_operator_is_constrained(db_conn, run):
    """`operator` encodes the rule for what counts as a parameter, so it is checked."""
    aid = run(_make_analysis(db_conn))

    async def _insert(operator: str):
        async with db_conn.begin() as conn:
            await conn.execute(text(
                "INSERT INTO analysis_parameters (analysis_id, name, kind, operator, value)"
                " VALUES (:aid, :name, 'diagnosis', :op, '[\"D66\"]'::jsonb)"
            ), {"aid": aid, "name": f"p_{operator}", "op": operator})

    for ok in ("in", "between", "gt", "gte", "lt", "lte", "eq"):
        run(_insert(ok))                       # every SQL shape the rule allows
    with pytest.raises(Exception):
        run(_insert("regex"))                  # not a comparison the rule recognises


def test_a_parameters_kind_is_open_but_its_operator_is_not(db_conn, run):
    """New domains must not need a migration; new operators should not exist."""
    aid = run(_make_analysis(db_conn))

    async def _insert(kind: str):
        async with db_conn.begin() as conn:
            await conn.execute(text(
                "INSERT INTO analysis_parameters (analysis_id, name, kind, operator, value)"
                " VALUES (:aid, :k, :k, 'in', '[1]'::jsonb)"
            ), {"aid": aid, "k": kind})

    for kind in ("date_range", "diagnosis", "medication", "orderset", "procedure",
                 "alert", "panel", "flowsheet_row", "something_invented_later"):
        run(_insert(kind))


async def _make_analysis(database) -> str:
    """A bare analyses row to hang parameters off."""
    async with database.begin() as conn:
        return str((await conn.execute(text(
            "INSERT INTO analyses (lineage_id, title) VALUES (gen_random_uuid(), 't')"
            " RETURNING id"))).scalar_one())


def test_every_report_can_have_its_own_figure_1(db_conn, run):
    """Captions are positional, so `Figure 1` recurs; (report, serial) is what is unique."""
    cid = str(uuid.uuid4())
    run(repo.create_conversation(cid, "a@b.c"))

    async def _report_with_figure(uid: str) -> str:
        rec = await repo.record_report(
            cid, title=uid, subtitle="", storage_backend="local", storage_key=f"{uid}.pdf",
            report_uid=uid, size_bytes=1, published_by=None, handling_marking=None,
            qcp_built_at=None, body_markdown=None)
        async with db_conn.begin() as conn:
            await conn.execute(text(
                "INSERT INTO figures (report_id, serial, label, chart_type, title)"
                " VALUES (:rid, 1, 'Figure 1', 'hbar', 'A chart')"
            ), {"rid": rec["id"]})
        return str(rec["id"])

    rid = run(_report_with_figure("rep-one"))
    run(_report_with_figure("rep-two"))          # same caption, different report: fine

    async def _duplicate_serial():
        async with db_conn.begin() as conn:
            await conn.execute(text(
                "INSERT INTO figures (report_id, serial, label, chart_type, title)"
                " VALUES (:rid, 1, 'Figure 1 again', 'vbar', 'Another')"
            ), {"rid": rid})

    with pytest.raises(Exception):               # two Figure 1s in ONE report
        run(_duplicate_serial())
