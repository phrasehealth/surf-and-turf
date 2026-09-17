"""End-to-end through the FastAPI app with the mock agent and mock Snowflake."""
import os

os.environ.setdefault("AGENT_MODE", "mock")
os.environ.setdefault("SNOWFLAKE_MODE", "mock")
os.environ.setdefault("REPORT_STORAGE", "local")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_conversation_roundtrip_publishes_pdf(tmp_path):
    with TestClient(app) as c:
        cid = c.post("/conversations").json()["conversation_id"]
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


def test_download_rejects_traversal():
    with TestClient(app) as c:
        assert c.get("/reports/..%2Fapp%2Fmain.py").status_code in (404, 422)
        assert c.get("/reports/nope.pdf").status_code == 404
