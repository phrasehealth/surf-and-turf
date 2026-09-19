#!/usr/bin/env python
"""Stage 1 — catalog introspection. Produces the L0 spine of the pack.

    python introspect.py [--work DIR]

Reads INFORMATION_SCHEMA only: no table is scanned, so this is metadata-cost
regardless of warehouse size. Per spec §7.2 this stage is *authoritative* for
what exists, what its columns are called, their types and nullability — later
stages may add meaning but may not contradict it.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.tools.snowflake_sql import SnowflakeBackend  # noqa: E402
from app.config import settings                        # noqa: E402

import config                                           # noqa: E402
from common import (Column, Relation, relations_to_json, save_facts,  # noqa: E402
                    normalize, raw_name_for, utcnow)

KIND = {"BASE TABLE": "base table", "VIEW": "view",
        "MATERIALIZED VIEW": "materialized view", "EXTERNAL TABLE": "external table"}


def _schema_filter(alias: str) -> str:
    parts = [f"{alias}.table_schema NOT IN ({', '.join(repr(s) for s in config.EXCLUDE_SCHEMAS)})"]
    if config.INCLUDE_SCHEMAS:
        inc = ", ".join(repr(s) for s in config.INCLUDE_SCHEMAS)
        parts.append(f"{alias}.table_schema IN ({inc})")
    return " AND ".join(parts)


async def introspect(backend: SnowflakeBackend, database: str) -> list[Relation]:
    rel_rows = await backend.run(f"""
        SELECT table_schema, table_name, table_type, row_count, bytes,
               to_char(last_altered, 'YYYY-MM-DD') AS last_altered
        FROM {database}.information_schema.tables t
        WHERE {_schema_filter('t')}
        ORDER BY 1, 2
    """)
    rels: dict[str, Relation] = {}
    for r in rel_rows:
        rel = Relation(
            schema=normalize(r["TABLE_SCHEMA"]),
            name=normalize(r["TABLE_NAME"]),
            kind=KIND.get(r["TABLE_TYPE"], (r["TABLE_TYPE"] or "").lower()),
            raw_name=raw_name_for(r["TABLE_NAME"]),
            row_count=int(r["ROW_COUNT"]) if r["ROW_COUNT"] is not None else None,
            bytes=int(r["BYTES"]) if r["BYTES"] is not None else None,
            last_altered=r["LAST_ALTERED"] or "",
        )
        rels[rel.addr] = rel

    col_rows = await backend.run(f"""
        SELECT table_schema, table_name, column_name, data_type, is_nullable,
               ordinal_position
        FROM {database}.information_schema.columns c
        WHERE {_schema_filter('c')}
        ORDER BY 1, 2, 6
    """)
    orphan_cols = 0
    for c in col_rows:
        addr = f"{normalize(c['TABLE_SCHEMA'])}.{normalize(c['TABLE_NAME'])}"
        rel = rels.get(addr)
        if rel is None:          # a column for a relation TABLES did not list
            orphan_cols += 1
            continue
        rel.columns.append(Column(
            schema=rel.schema, relation=rel.name,
            name=normalize(c["COLUMN_NAME"]),
            type=(c["DATA_TYPE"] or "").upper(),
            nullable=(c["IS_NULLABLE"] or "").upper(),
            ordinal=int(c["ORDINAL_POSITION"] or 0),
            raw_name=raw_name_for(c["COLUMN_NAME"]),
        ))
    if orphan_cols:
        print(f"  ! {orphan_cols} column(s) referenced a relation not in TABLES; skipped")
    return list(rels.values())


async def main_async(work: Path, database: str) -> int:
    if settings.snowflake_mode != "real":
        raise SystemExit("SNOWFLAKE_MODE must be 'real' to introspect")
    if not database:
        raise SystemExit("--database is required")

    backend = SnowflakeBackend(database)
    print(f"introspecting {database} ...")
    rels = await introspect(backend, database)

    by_schema: dict[str, int] = {}
    for r in rels:
        by_schema[r.schema] = by_schema.get(r.schema, 0) + 1
    ncols = sum(len(r.columns) for r in rels)
    for s in sorted(by_schema):
        print(f"  {s:<10} {by_schema[s]:>5} relations")
    print(f"  {'total':<10} {len(rels):>5} relations, {ncols} columns")

    save_facts(work, "introspect", {
        "source": f"snowflake information_schema of {database}",
        "introspected_at": utcnow(),
        # The concrete database is recorded as provenance only; the rendered pack
        # writes {database} placeholders (spec §4.2).
        "database_introspected": database,
        "relations": relations_to_json(rels),
    })
    print(f"  -> {work / 'introspect.json'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="work directory")
    ap.add_argument("--database", required=True, help="the database to describe")
    args = ap.parse_args()
    return asyncio.run(main_async(Path(args.work), args.database))


if __name__ == "__main__":
    raise SystemExit(main())
