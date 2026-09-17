"""Snowflake access exposed to the agent as a small, fixed-interface toolset.

Three tools are registered on the in-process MCP server:

  run_sql(sql)             -> rows (read-only, LIMIT-capped, timeout-bounded)
  list_tables(pattern?)    -> tables/views visible to the configured role
  describe_table(name)     -> columns and types

The guard in `validate_readonly()` is deliberately conservative: a single
SELECT / WITH statement, no DDL/DML keywords, no statement chaining.  The
Snowflake role itself should *also* be read-only; this is defence in depth,
not the only line of defence.

SNOWFLAKE_MODE=mock serves canned data so the whole stack runs offline.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from claude_agent_sdk import tool

from ..config import settings

# ---------------------------------------------------------------------------
# Read-only guard
# ---------------------------------------------------------------------------

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|truncate|drop|alter|create|grant|revoke|"
    r"call|copy|put|get|remove|use|set|unset|begin|commit|rollback|execute)\b",
    re.IGNORECASE,
)
_LIMIT_RE = re.compile(r"\blimit\s+(\d+)\s*$", re.IGNORECASE)


class UnsafeSQL(ValueError):
    pass


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql.strip()


def validate_readonly(sql: str, row_limit: int) -> str:
    """Return a sanitized, LIMIT-capped statement or raise UnsafeSQL."""
    clean = _strip_comments(sql).rstrip(";").strip()
    if not clean:
        raise UnsafeSQL("Empty statement.")
    if ";" in clean:
        raise UnsafeSQL("Only a single statement is allowed.")
    if not re.match(r"^(select|with)\b", clean, re.IGNORECASE):
        raise UnsafeSQL("Only SELECT / WITH queries are allowed.")
    if _FORBIDDEN.search(clean):
        raise UnsafeSQL("Statement contains a forbidden keyword (read-only tool).")

    m = _LIMIT_RE.search(clean)
    if m:
        if int(m.group(1)) > row_limit:
            clean = _LIMIT_RE.sub(f"LIMIT {row_limit}", clean)
    else:
        clean = f"{clean}\nLIMIT {row_limit}"
    return clean


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


class MockBackend:
    """Deterministic sample data so the stack runs without Snowflake."""

    TABLES = {
        "ANALYTICS.CDS.ALERT_FIRINGS": [
            ("ALERT_ID", "VARCHAR"), ("ALERT_NAME", "VARCHAR"), ("FIRED_AT", "TIMESTAMP_NTZ"),
            ("FACILITY", "VARCHAR"), ("ACCEPTED", "BOOLEAN"),
        ],
        "ANALYTICS.CDS.ALERT_DAILY": [
            ("DAY", "DATE"), ("ALERT_NAME", "VARCHAR"), ("FIRINGS", "NUMBER"),
            ("ACCEPT_RATE", "FLOAT"),
        ],
    }

    async def run(self, sql: str) -> list[dict[str, Any]]:
        await asyncio.sleep(0.2)
        return [
            {"DAY": f"2026-09-{d:02d}", "ALERT_NAME": "Sepsis Screen", "FIRINGS": 120 + d * 3,
             "ACCEPT_RATE": round(0.31 + d * 0.004, 3)}
            for d in range(1, 8)
        ]

    async def list_tables(self, pattern: str | None) -> list[dict[str, str]]:
        out = []
        for full in self.TABLES:
            if pattern and pattern.lower() not in full.lower():
                continue
            db, sch, name = full.split(".")
            out.append({"database": db, "schema": sch, "name": name, "kind": "TABLE"})
        return out

    async def describe(self, name: str) -> list[dict[str, str]]:
        cols = self.TABLES.get(name.upper())
        if cols is None:
            raise ValueError(f"Unknown table {name}")
        return [{"name": c, "type": t} for c, t in cols]


class SnowflakeBackend:
    """Thin wrapper over snowflake-connector-python; one connection per call.

    Simple and safe for a handful of concurrent report sessions.  Add a pool
    if you see connection setup dominating query time.
    """

    def _connect(self):
        import snowflake.connector  # imported lazily so mock mode has no dependency

        kwargs: dict[str, Any] = dict(
            account=settings.snowflake_account,
            user=settings.snowflake_user,
            role=settings.snowflake_role,
            warehouse=settings.snowflake_warehouse,
            database=settings.snowflake_database or None,
            schema=settings.snowflake_schema or None,
            session_parameters={"QUERY_TAG": "report-agent"},
            client_session_keep_alive=False,
        )
        if settings.snowflake_private_key_path:
            from cryptography.hazmat.primitives import serialization

            with open(settings.snowflake_private_key_path, "rb") as f:
                key = serialization.load_pem_private_key(f.read(), password=None)
            kwargs["private_key"] = key.private_bytes(
                serialization.Encoding.DER,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        else:
            kwargs["password"] = settings.snowflake_password
        return snowflake.connector.connect(**kwargs)

    def _query(self, sql: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {settings.sql_timeout_s}")
            cur.execute(sql)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, (_jsonable(v) for v in row))) for row in cur.fetchall()]
        finally:
            conn.close()

    async def run(self, sql: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._query, sql)

    async def list_tables(self, pattern: str | None) -> list[dict[str, str]]:
        like = f"LIKE '%{pattern}%'" if pattern else ""
        rows = await self.run(
            f"SELECT table_catalog, table_schema, table_name, table_type "
            f"FROM information_schema.tables WHERE table_schema <> 'INFORMATION_SCHEMA' "
            f"{'AND table_name ' + like if like else ''} ORDER BY 1,2,3 LIMIT 500"
        )
        return [
            {"database": r["TABLE_CATALOG"], "schema": r["TABLE_SCHEMA"],
             "name": r["TABLE_NAME"], "kind": r["TABLE_TYPE"]}
            for r in rows
        ]

    async def describe(self, name: str) -> list[dict[str, str]]:
        rows = await asyncio.to_thread(self._query, f"DESCRIBE TABLE {name}")
        return [{"name": r["name"], "type": r["type"]} for r in rows]


def get_backend():
    return SnowflakeBackend() if settings.snowflake_mode == "real" else MockBackend()


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def _text(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


def _error(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def build_tools():
    backend = get_backend()

    @tool(
        "run_sql",
        "Run a read-only SELECT against Snowflake. Returns JSON rows (capped at "
        f"{settings.sql_row_limit}); aggregate in SQL rather than pulling raw rows.",
        {"sql": str},
    )
    async def run_sql(args: dict[str, Any]) -> dict[str, Any]:
        try:
            sql = validate_readonly(args["sql"], settings.sql_row_limit)
        except UnsafeSQL as e:
            return _error(f"Rejected: {e}")
        try:
            rows = await backend.run(sql)
        except Exception as e:  # surface Snowflake errors to the model, not the user
            return _error(f"Snowflake error: {e}")
        return _text({"row_count": len(rows), "rows": rows})

    @tool(
        "list_tables",
        "List tables and views visible to the reporting role, optionally filtered by a substring.",
        {"pattern": str},
    )
    async def list_tables(args: dict[str, Any]) -> dict[str, Any]:
        return _text(await backend.list_tables(args.get("pattern") or None))

    @tool(
        "describe_table",
        "Return the columns and types of a table. Use fully-qualified DATABASE.SCHEMA.TABLE.",
        {"table": str},
    )
    async def describe_table(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _text(await backend.describe(args["table"]))
        except Exception as e:
            return _error(str(e))

    return [run_sql, list_tables, describe_table]
