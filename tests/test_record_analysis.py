"""record_analysis: adopting a result, versioning a correction, gating the publish."""
from __future__ import annotations

import json
import uuid

import pytest

from app.db import repository as repo
from app.tools import publish_report as publish_tool
from app.tools import record_analysis as record_tool
from app.tools.result_cache import ResultCache, bind_template, matches


def _payload(res):
    return json.loads(res["content"][0]["text"])


@pytest.fixture
def conversation(db_conn, run):
    cid = str(uuid.uuid4())
    run(repo.create_conversation(cid, "d@x.com"))
    return cid


@pytest.fixture
def results():
    c = ResultCache()
    c.put("SELECT dx_code, count(*) FROM gold.icd_diagnoses WHERE dx_code IN ('D66') "
          "GROUP BY 1", [{"DX_CODE": "D66", "N": 42}], 120)
    c.bind("toolu_aaa", "SELECT dx_code, count(*) FROM gold.icd_diagnoses "
                        "WHERE dx_code IN ('D66') GROUP BY 1")
    return c


SPEC = {
    "title": "Hemophilia A patients",
    "subtitle": "Counts by diagnosis code",
    "note_template": "Data included all dates. Source: penn, gold.icd_diagnoses.",
    "queries": [{"tool_use_id": "toolu_aaa", "primary": True,
                 "sql_template": "SELECT dx_code, count(*) FROM gold.icd_diagnoses "
                                 "WHERE dx_code IN (:dx_codes) GROUP BY 1"}],
    "parameters": [{"name": "dx_codes", "kind": "diagnosis", "operator": "in",
                    "value": ["D66"], "expression": "ICD-10 D66",
                    "label": "Patient population"}],
    "relations": ["gold.icd_diagnoses"],
    "joins": [],
    "chart_type": "hbar",
    "chart_spec": {"label": "DX_CODE", "value": "N"},
}


# ----------------------------------------------------------------- adopting

def test_records_an_analysis_by_adopting_a_prior_result(conversation, results, run):
    t = record_tool.build_tool(conversation, results, author="d@x.com")
    out = _payload(run(t.handler(dict(SPEC))))
    assert out["status"] == "recorded"
    assert out["label"].startswith("A-") and out["label"].endswith(".1")
    # The template binds back to exactly what ran, so a refresh reproduces it.
    assert "note" not in out

    cart = run(repo.list_analyses(conversation))
    assert [a["title"] for a in cart] == ["Hemophilia A patients"]
    assert cart[0]["chart_type"] == "hbar" and cart[0]["latest_run_id"] is not None


def test_refuses_when_the_result_was_never_cached(conversation, results, run):
    t = record_tool.build_tool(conversation, results, author="d@x.com")
    spec = {**SPEC, "queries": [{"tool_use_id": "toolu_missing",
                                 "sql_template": "SELECT 1 FROM nowhere"}]}
    res = run(t.handler(spec))
    assert res["is_error"]
    text = res["content"][0]["text"]
    assert "toolu_missing" in text and "run_sql" in text, "the refusal must say how to fix it"


def test_an_approximate_template_is_recorded_not_rejected(conversation, results, run):
    """Templates only have to be close (docs §7); the discrepancy is surfaced."""
    t = record_tool.build_tool(conversation, results, author="d@x.com")
    spec = {**SPEC, "queries": [{**SPEC["queries"][0],
                                 "sql_template": "SELECT dx_code, count(*) AS n FROM "
                                                 "gold.icd_diagnoses WHERE dx_code "
                                                 "IN (:dx_codes) GROUP BY dx_code"}]}
    out = _payload(run(t.handler(spec)))
    assert out["status"] == "recorded"
    assert "note" in out and "refresh" in out["note"]


def test_exactly_one_query_may_be_primary(conversation, results, run):
    t = record_tool.build_tool(conversation, results, author="d@x.com")
    q = SPEC["queries"][0]
    res = run(t.handler({**SPEC, "queries": [dict(q), {**q, "primary": True}]}))
    assert res["is_error"] and "primary" in res["content"][0]["text"]


def test_a_single_query_needs_no_primary_flag(conversation, results, run):
    t = record_tool.build_tool(conversation, results, author="d@x.com")
    spec = {**SPEC, "queries": [{k: v for k, v in SPEC["queries"][0].items()
                                 if k != "primary"}]}
    assert _payload(run(t.handler(spec)))["status"] == "recorded"


# ---------------------------------------------------------------- versioning

def test_a_correction_makes_a_new_version_of_the_same_lineage(conversation, results, run):
    t = record_tool.build_tool(conversation, results, author="d@x.com")
    first = _payload(run(t.handler(dict(SPEC))))["label"]
    second = _payload(run(t.handler({**SPEC, "title": "Hemophilia A patients (corrected)",
                                     "supersedes": first})))["label"]

    assert second.endswith(".2"), "a correction is version 2, not a new analysis"
    cart = run(repo.list_analyses(conversation))
    assert len(cart) == 1, "only the current version is offered"
    assert cart[0]["title"].endswith("(corrected)")


def test_superseding_an_unknown_label_is_refused(conversation, results, run):
    t = record_tool.build_tool(conversation, results, author="d@x.com")
    res = run(t.handler({**SPEC, "supersedes": "A-999999.1"}))
    assert res["is_error"] and "999999" in res["content"][0]["text"]


# -------------------------------------------------------------- publish gate

def test_publish_is_refused_until_the_analyses_are_recorded(conversation, results, run):
    pub = publish_tool.build_tool(conversation, None, author="d@x.com")
    res = run(pub.handler({"title": "Report", "subtitle": "",
                           "body_markdown": "## One\n\nnumbers\n\n## Two\n\nmore numbers"}))
    assert res["is_error"]
    text = res["content"][0]["text"]
    assert "record_analysis" in text, "the refusal must name the tool"
    assert "tool_use_id" in text, "and say to reuse the queries already run"
    assert "publish_report again" in text, "and say what to do next"


def test_publish_rejects_a_label_from_another_conversation(conversation, results, db_conn, run):
    other = str(uuid.uuid4())
    run(repo.create_conversation(other, "d@x.com"))
    label = _payload(run(record_tool.build_tool(other, results).handler(dict(SPEC))))["label"]

    pub = publish_tool.build_tool(conversation, None, author="d@x.com")
    res = run(pub.handler({"title": "R", "subtitle": "", "analyses": [label],
                           "body_markdown": "## One\n\nnumbers here to pass the check"}))
    assert res["is_error"] and label in res["content"][0]["text"]


def test_publishing_links_the_report_to_the_runs_it_shows(conversation, results, db_conn, run):
    from sqlalchemy import text

    label = _payload(run(record_tool.build_tool(conversation, results,
                                                author="d@x.com").handler(dict(SPEC))))["label"]
    pub = publish_tool.build_tool(conversation, None, author="d@x.com")
    out = _payload(run(pub.handler({
        "title": "Hemophilia report", "subtitle": "For the haematology service",
        "analyses": [label],
        "body_markdown": "## Hemophilia A patients\n\n| code | n |\n|---|---:|\n| D66 | 42 |"})))
    assert out["status"] == "published"

    async def _contents():
        async with db_conn.begin() as conn:
            return (await conn.execute(text(
                "SELECT r.title, rc.position, a.title AS analysis_title"
                "  FROM reports r JOIN report_contents rc ON rc.report_id = r.id"
                "  JOIN analysis_runs ar ON ar.id = rc.analysis_run_id"
                "  JOIN analyses a ON a.id = ar.analysis_id"))).mappings().all()

    rows = run(_contents())
    assert len(rows) == 1
    assert rows[0]["position"] == 1
    assert rows[0]["analysis_title"] == "Hemophilia A patients"


# ------------------------------------------------------------- template match

def test_binding_a_template_reproduces_a_list_filter():
    tpl = "WHERE dx_code IN (:dx) AND d BETWEEN :a AND :b"
    bound = bind_template(tpl, {"dx": ["D66", "D66.0"], "a": "2026-01-01", "b": "2026-09-01"})
    assert bound == "WHERE dx_code IN ('D66', 'D66.0') AND d BETWEEN '2026-01-01' AND '2026-09-01'"
    assert matches(tpl, {"dx": ["D66", "D66.0"], "a": "2026-01-01", "b": "2026-09-01"}, bound)


def test_template_match_ignores_whitespace_but_not_meaning():
    tpl = "SELECT a FROM t WHERE x IN (:v)"
    assert matches(tpl, {"v": [1]}, "SELECT   a\nFROM t\nWHERE x IN (1)")
    assert not matches(tpl, {"v": [1]}, "SELECT a FROM t WHERE x IN (2)")


# --------------------------------------------------------------- the cache

def test_cache_evicts_by_count_and_age():
    c = ResultCache(max_entries=2, ttl_s=1000)
    for i in range(3):
        c.put(f"SELECT {i}", [{"n": i}], 1)
    assert len(c) == 2 and c.get_by_sql("SELECT 0") is None

    stale = ResultCache(max_entries=10, ttl_s=-1)
    stale.put("SELECT 1", [{"n": 1}], 1)
    assert stale.get_by_sql("SELECT 1") is None, "an aged-out result is not adopted"


def test_cache_lookup_survives_reformatted_sql():
    c = ResultCache()
    c.put("SELECT a FROM t", [{"a": 1}], 1)
    assert c.get_by_sql("SELECT   a\n  FROM t") is not None
