import pytest

from app.tools.snowflake_sql import UnsafeSQL, validate_readonly


def test_adds_limit():
    assert validate_readonly("select 1", 100).endswith("LIMIT 100")


def test_caps_existing_limit():
    assert validate_readonly("select * from t limit 5000", 500).endswith("LIMIT 500")


def test_keeps_smaller_limit():
    assert validate_readonly("select * from t limit 10", 500).lower().endswith("limit 10")


def test_with_cte_allowed():
    validate_readonly("with x as (select 1 a) select a from x", 10)


def test_strips_comments_and_semicolon():
    out = validate_readonly("-- hi\nselect 1; /* trailing */", 10)
    assert ";" not in out


@pytest.mark.parametrize("sql", [
    "delete from t",
    "select 1; drop table t",
    "insert into t select 1",
    "select * from t; select 2",
    "create table x as select 1",
    "call proc()",
    "",
    "-- only a comment",
])
def test_rejects(sql):
    with pytest.raises(UnsafeSQL):
        validate_readonly(sql, 10)
