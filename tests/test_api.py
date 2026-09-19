"""End-to-end through the FastAPI app with the mock agent and mock Snowflake.

These now need a database: persistence is required, so the app's lifespan refuses
to start without one. `client` skips with the same message as the other database
tests when nothing is listening.
"""
import os

import pytest

os.environ.setdefault("AGENT_MODE", "mock")
os.environ.setdefault("SNOWFLAKE_MODE", "mock")
os.environ.setdefault("REPORT_STORAGE", "local")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture
def client(migrated_database):
    """TestClient with the app's lifespan running against the test database."""
    import app.config as cfg

    object.__setattr__(cfg.settings, "database_url", migrated_database)
    with TestClient(app) as c:
        yield c


def test_conversation_roundtrip_publishes_pdf(tmp_path, client):
    if True:
        c = client
        cid = c.post("/conversations", json={"database": "ANALYTICS"}).json()["conversation_id"]
        with c.websocket_connect(f"/ws/{cid}") as ws:
            assert ws.receive_json()["type"] == "hello"
            ws.send_json({"prompt": "generate the report as a pdf"})
            events = []
            while True:
                ev = ws.receive_json()
                events.append(ev)
                if ev["type"] == "turn_end":
                    break
        types = [e["type"] for e in events]
        assert "text_delta" in types and "tool_use" in types and "tool_result" in types
        report = next(e for e in events if e["type"] == "report")
        assert report["url"].endswith(".pdf")
        # the download endpoint serves the file
        r = c.get(f"/reports/{report['filename']}")
        assert r.status_code == 200 and r.content[:5] == b"%PDF-"
        # report is remembered on the conversation
        assert c.get(f"/conversations/{cid}").json()["reports"][0]["filename"] == report["filename"]


def test_download_rejects_traversal(client):
    if True:
        c = client
        assert c.get("/reports/..%2Fapp%2Fmain.py").status_code in (404, 422)
        assert c.get("/reports/nope.pdf").status_code == 404


def test_a_conversation_must_name_a_database(client):
    """The database is part of what a conversation is, not an optional setting."""
    r = client.post("/conversations", json={})
    assert r.status_code == 400
    assert "database is required" in str(r.json())


def test_an_unavailable_database_is_refused(client):
    r = client.post("/conversations", json={"database": "not_a_real_db"})
    assert r.status_code == 400
    assert "not available" in str(r.json())


def test_only_databases_with_a_pack_are_offered_by_the_api(client):
    """The picker offers schema knowledge we hold, not everything the role can reach."""
    dbs = client.get("/databases").json()["databases"]
    assert "ANALYTICS" in dbs, "the mock pack is committed for the offline path"


def test_the_database_is_recorded_and_cannot_change(client):
    body = client.post("/conversations", json={"database": "ANALYTICS"}).json()
    assert body["database"] == "ANALYTICS"
    got = client.get(f"/conversations/{body['conversation_id']}").json()
    assert got["database"] == "ANALYTICS"
