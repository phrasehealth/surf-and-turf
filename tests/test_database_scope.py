"""A conversation is bound to one database, and every statement is held to it.

The restriction is per query, not merely per connection: a fully-qualified name
reaches straight past whatever the connection defaulted to. This is the failure the
real agent hit — configured for one database, it read the pack's manifest and
hard-coded another, and nothing stopped it.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.agent import MockAgentSession, manager
from app.db import repository as repo
from app.tools.snowflake_sql import UnsafeSQL, cross_database_refs, validate_readonly


@pytest.mark.parametrize("sql", [
    "SELECT * FROM gold.alert_events",                       # two-part: the norm
    "SELECT * FROM baptist.gold.alert_events",               # its own database
    "SELECT * FROM BAPTIST.GOLD.ALERT_EVENTS",               # case does not matter
    "SELECT a FROM gold.t WHERE note = 'penn.gold.x'",       # a literal, not a table
    "SELECT a FROM gold.t -- see penn.gold.x",               # a comment, not a table
])
def test_statements_within_the_conversations_database_are_allowed(sql):
    assert validate_readonly(sql, 500, "baptist")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM penn.gold.alert_events",
    "SELECT a FROM gold.t JOIN penn.gold.u ON t.id = u.id",
    "WITH x AS (SELECT * FROM uvm.gold.alert_events) SELECT * FROM x",
])
def test_reaching_into_another_database_is_refused(sql):
    with pytest.raises(UnsafeSQL) as e:
        validate_readonly(sql, 500, "baptist")
    msg = str(e.value)
    assert "baptist" in msg, "the refusal names the database in force"
    assert "new conversation" in msg, "and says how to query a different one"


def test_the_refusal_names_the_offending_reference():
    refs = cross_database_refs(
        "SELECT * FROM penn.gold.a JOIN uvm.gold.b ON 1=1", "baptist")
    assert refs == ["penn.gold.a", "uvm.gold.b"]


def test_no_restriction_when_no_database_is_bound():
    """Tooling paths that predate the binding keep working."""
    assert validate_readonly("SELECT * FROM penn.gold.t", 500, "")


# ------------------------------------------------------------------ the session

def test_a_session_cannot_change_database():
    s = MockAgentSession("c1", user_id="d@x.com", database="penn")
    assert s.database == "penn"
    with pytest.raises(AttributeError):
        s.database = "baptist"          # read-only property, deliberately


def test_a_conversation_must_name_a_database():
    with pytest.raises(ValueError, match="must name the database"):
        manager.create(user_id="d@x.com")


def test_a_revived_conversation_keeps_its_database(db_conn, run):
    """Revival restores the binding; it does not re-choose it."""
    cid = str(uuid.uuid4())
    run(repo.create_conversation(cid, "d@x.com", "penn"))
    manager.sessions.pop(cid, None)

    revived = run(manager.revive(cid))
    assert revived is not None and revived.database == "penn"
    manager.sessions.pop(cid, None)


def test_the_database_is_stored_with_the_conversation(db_conn, run):
    cid = str(uuid.uuid4())
    run(repo.create_conversation(cid, "d@x.com", "baptist"))
    assert run(repo.get_conversation(cid))["database"] == "baptist"


# ------------------------------------------------- one pack, one database

def test_only_databases_with_a_pack_are_offered(tmp_path, workspace_at):
    """Offering a database we have no schema for would hand the agent the wrong pack."""
    from app import agent as agent_mod

    for name in ("penn", "baptistmemorial"):
        (tmp_path / name / "qcp").mkdir(parents=True)
        (tmp_path / name / "qcp" / "MANIFEST.md").write_text(f"pack_name: {name}\n")
    (tmp_path / "temple").mkdir()                     # a directory, but no pack
    (tmp_path / "CLAUDE.md").write_text("shared")     # not a database

    workspace_at(tmp_path)
    assert agent_mod.available_databases() == ["BAPTISTMEMORIAL", "PENN"]


def test_the_workspace_is_the_conversations_database():
    from app.agent import workspace_for
    from app.config import settings

    assert workspace_for("PENN") == Path(settings.workspace_dir) / "penn"
    assert workspace_for("BaptistMemorial").name == "baptistmemorial"


def test_a_session_is_confined_to_its_own_database(tmp_path, workspace_at):
    """The guard is the boundary: another tenant's pack is as off-limits as a secret."""
    from app.agent import workspace_for
    from app.workspace_guard import check

    workspace_at(tmp_path)
    mine = workspace_for("penn")

    assert check("Read", {"file_path": "qcp/index.md"}, mine)[0]
    allowed, reason = check("Read", {"file_path": "../baptistmemorial/qcp/columns.tsv"}, mine)
    assert not allowed and "outside the workspace" in reason
    assert not check("Grep", {"pattern": "x", "path": "../baptistmemorial"}, mine)[0]


def test_the_pack_reaches_the_model_through_the_system_prompt():
    """CLAUDE.md is shared and cannot vary, so the per-database pack goes here."""
    from app.agent import AgentSession

    opts = AgentSession("c1", user_id="d@x.com", database="PENN")._build_options()
    appended = opts.system_prompt["append"]
    assert "--- qcp/index.md ---" in appended
    assert "--- qcp/README.md ---" in appended
    assert "PENN database and cannot change it" in appended
    assert opts.cwd.endswith("/penn")
