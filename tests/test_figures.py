"""Charts: drawn by code from recorded numbers, captioned by position at publish."""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from app.chart_render import ChartError, render
from app.db import repository as repo
from app.tools import publish_report as publish_tool
from app.tools import record_analysis as record_tool
from app.tools.result_cache import ResultCache

ROWS = [{"MASTER_TYPE": "LGL", "ALERT_COUNT": 1204882},
        {"MASTER_TYPE": "EMI", "ALERT_COUNT": 431009},
        {"MASTER_TYPE": "MED", "ALERT_COUNT": 180442}]
SQL = "SELECT master_type, COUNT(*) AS alert_count FROM gold.alert_events GROUP BY 1"


@pytest.fixture
def results():
    c = ResultCache()
    c.put(SQL, ROWS, 120)
    return c


def spec(**over):
    base = {
        "title": "Alert firings by type",
        "subtitle": "August 2026",
        "note_template": "Filtered to firing dates between 2026-08-01 and 2026-09-01.",
        "queries": [{"result_ref": "q1", "sql_template": SQL, "primary": True}],
        "parameters": [], "relations": ["gold.alert_events"], "joins": [],
        "chart_type": "hbar",
        "chart_spec": {"category": "master_type", "value": "alert_count"},
    }
    base.update(over)
    return base


@pytest.fixture
def conversation(db_conn, run):
    cid = str(uuid.uuid4())
    run(repo.create_conversation(cid, "d@x.com", "penn"))
    return cid


# ------------------------------------------------------------- the renderer

def test_the_code_picks_the_colour_not_the_model():
    svg = render("hbar", {"category": "master_type", "value": "alert_count"}, ROWS)
    assert "<svg" in svg and svg.count("<path") == 3, "one bar per row"
    assert any(c in svg for c in ("#9027db", "#a753ff", "#bc9bff")), \
        "the palette comes from charts.py"


def test_a_column_the_query_did_not_return_is_reported_clearly():
    with pytest.raises(ChartError) as e:
        render("hbar", {"category": "nope", "value": "alert_count"}, ROWS)
    msg = str(e.value)
    assert "nope" in msg and "MASTER_TYPE" in msg, "say what was asked for and what exists"


def test_an_unknown_chart_type_lists_the_known_ones():
    with pytest.raises(ChartError, match="hbar"):
        render("piechart3d", {}, ROWS)


def test_column_lookup_is_case_insensitive():
    """Snowflake returns upper-cased keys; the agent writes the lower-case name."""
    assert render("hbar", {"category": "MaStEr_TyPe", "value": "alert_count"}, ROWS)


def test_empty_results_are_refused_rather_than_drawn_empty():
    with pytest.raises(ChartError, match="no rows"):
        render("hbar", {"category": "master_type", "value": "alert_count"}, [])


# --------------------------------------------------------- recording a chart

def test_recording_draws_and_stores_the_chart(conversation, results, db_conn, run):
    t = record_tool.build_tool(conversation, results, author="d@x.com", database="penn")
    out = json.loads(run(t.handler(spec()))["content"][0]["text"])
    assert out["chart"] == "hbar"

    async def _svg():
        async with db_conn.begin() as conn:
            return (await conn.execute(text(
                "SELECT chart_svg, chart_error FROM analysis_runs"))).mappings().one()
    row = run(_svg())
    assert row["chart_svg"].startswith("<svg") and row["chart_error"] is None


def test_a_bad_spec_still_records_the_analysis(conversation, results, db_conn, run):
    """The numbers are worth keeping even when the chart cannot be drawn."""
    t = record_tool.build_tool(conversation, results, author="d@x.com", database="penn")
    out = json.loads(run(t.handler(spec(chart_spec={"category": "wrong",
                                                    "value": "alert_count"})))
                     ["content"][0]["text"])
    assert out["status"] == "recorded"
    assert "not drawn" in out["chart"], "and the agent is told why"

    async def _row():
        async with db_conn.begin() as conn:
            return (await conn.execute(text(
                "SELECT chart_svg, chart_error FROM analysis_runs"))).mappings().one()
    row = run(_row())
    assert row["chart_svg"] is None and "wrong" in row["chart_error"]


# ----------------------------------------------------------- placing figures

def test_publishing_captions_figures_by_position(conversation, results, db_conn, run):
    rec = record_tool.build_tool(conversation, results, author="d@x.com", database="penn")
    a1 = json.loads(run(rec.handler(spec()))["content"][0]["text"])["label"]
    a2 = json.loads(run(rec.handler(spec(title="Second analysis")))
                    ["content"][0]["text"])["label"]

    pub = publish_tool.build_tool(conversation, None, author="d@x.com", database="penn")
    out = json.loads(run(pub.handler({
        "title": "Alert review", "subtitle": "August", "analyses": [a1, a2],
        "body_markdown": "## Alert firings by type\n\ntext\n\n## Second analysis\n\nmore",
    }))["content"][0]["text"])
    assert out["status"] == "published"

    async def _figures():
        async with db_conn.begin() as conn:
            return (await conn.execute(text(
                "SELECT serial, label, title, chart_type FROM figures ORDER BY serial"
            ))).mappings().all()
    figs = run(_figures())
    assert [f["label"] for f in figs] == ["Figure 1", "Figure 2"]
    assert [f["title"] for f in figs] == ["Alert firings by type", "Second analysis"]


def test_the_model_never_writes_a_figure_number(conversation, results, db_conn, run):
    """The body has no number in it; the caption is added when the position is known."""
    rec = record_tool.build_tool(conversation, results, author="d@x.com", database="penn")
    label = json.loads(run(rec.handler(spec()))["content"][0]["text"])["label"]
    pub = publish_tool.build_tool(conversation, None, author="d@x.com", database="penn")
    body = "## Alert firings by type\n\nNo figure number written here."
    run(pub.handler({"title": "R", "subtitle": "", "analyses": [label],
                     "body_markdown": body}))

    async def _stored():
        async with db_conn.begin() as conn:
            return (await conn.execute(text(
                "SELECT body_markdown FROM reports"))).scalar_one()
    assert "Figure 1" not in run(_stored()), "the stored body is what the model wrote"

    async def _label():
        async with db_conn.begin() as conn:
            return (await conn.execute(text("SELECT label FROM figures"))).scalar_one()
    assert run(_label()) == "Figure 1", "the caption is the server's"
