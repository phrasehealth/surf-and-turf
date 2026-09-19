#!/usr/bin/env python
"""Stage 3 — measurement. Everything here scans data and costs warehouse time.

    python measure.py freshness [--force] [--schema gold]
    python measure.py joins --dry-run          # candidate counts only, no queries
    python measure.py joins [--max-pairs N]
    python measure.py profiles --schema gold [--relation alert_events]

Each sub-stage is opt-in and independently re-runnable, and each writes into
work/measure.json without disturbing the others. Relations larger than
config.MAX_SCAN_BYTES are skipped unless --force, so a full run cannot
accidentally scan a 60 GB table.

Order matters (spec §7.5): freshness is cheapest and prevents the worst failure
(a confident report from a dead table); profiles are dearest and prevent the
mildest (an extra round trip on a filter).
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.tools.snowflake_sql import SnowflakeBackend   # noqa: E402
from app.config import settings                        # noqa: E402

import config                                          # noqa: E402
from common import (load_facts, relations_from_json, save_facts,  # noqa: E402
                    utcnow, human_count)

DB = "{db}"  # replaced per query with the live database


def q_ident(schema: str, relation: str) -> str:
    return f"{settings.snowflake_database}.{schema}.{relation}"


def scannable(rel, force: bool) -> bool:
    if force or rel.bytes is None:
        return True
    return rel.bytes <= config.MAX_SCAN_BYTES


def _merge(work: Path, key: str, value) -> None:
    facts = load_facts(work, "measure") or {}
    facts.pop("stage", None); facts.pop("written_at", None)
    facts[key] = value
    save_facts(work, "measure", facts)


# --------------------------------------------------------------------------
# freshness
# --------------------------------------------------------------------------

async def run_freshness(backend, rels, force: bool, schemas: list[str]) -> dict:
    out, skipped = {}, 0
    targets = [r for r in rels if (not schemas or r.schema in schemas)
               and any(c.type in config.DATE_TYPES for c in r.columns)
               and r.row_count]
    print(f"freshness: {len(targets)} relation(s) with a date column and rows")
    for i, rel in enumerate(targets, 1):
        if not scannable(rel, force):
            skipped += 1
            continue
        dcols = [c.name for c in rel.columns if c.type in config.DATE_TYPES]
        sel = ", ".join(f"to_char(max({c}), 'YYYY-MM-DD') AS {c}" for c in dcols)
        try:
            rows = await backend.run(f"SELECT {sel} FROM {q_ident(rel.schema, rel.name)}")
        except Exception as e:
            print(f"  ! {rel.addr}: {str(e).splitlines()[0][:90]}")
            continue
        maxes = {k.lower(): v for k, v in (rows[0] if rows else {}).items() if v}
        if maxes:
            # The newest business date across the relation's date columns, and
            # which column produced it. Picking *which* column means "business
            # date" is an inference; the value itself is measured.
            col, val = max(maxes.items(), key=lambda kv: kv[1])
            out[rel.addr] = {"column": col, "max": val, "all": maxes,
                             "measured_on": date.today().isoformat()}
        if i % 25 == 0:
            print(f"  {i}/{len(targets)} ...")
    print(f"  measured {len(out)}, skipped {skipped} over the "
          f"{config.MAX_SCAN_BYTES // 1024**3} GB scan budget")
    return out


# --------------------------------------------------------------------------
# joins
# --------------------------------------------------------------------------

def join_candidates(rels, schemas: list[str]):
    """Key-shaped column names shared by more than one relation, minus plumbing."""
    scoped = [r for r in rels if not schemas or r.schema in schemas]
    by_col = defaultdict(list)
    for r in scoped:
        for c in r.columns:
            if c.name in config.JOIN_DENYLIST:
                continue
            if not c.name.endswith(config.JOIN_KEY_SUFFIXES):
                continue
            by_col[c.name].append((r, c))
    return {col: hits for col, hits in by_col.items() if len(hits) > 1}


async def column_stats(backend, cache, rel, col) -> dict | None:
    key = f"{rel.addr}.{col}"
    if key in cache:
        return cache[key]
    try:
        rows = await backend.run(
            f"SELECT count(*) n, count({col}) nn, approx_count_distinct({col}) d "
            f"FROM {q_ident(rel.schema, rel.name)}")
    except Exception as e:
        print(f"  ! stats {key}: {str(e).splitlines()[0][:80]}")
        cache[key] = None
        return None
    r = rows[0]
    n, nn, d = int(r["N"]), int(r["NN"]), int(r["D"])
    cache[key] = {"rows": n, "non_null": nn, "distinct": d,
                  "unique": bool(nn and d >= nn * 0.999)}
    return cache[key]


async def run_joins(backend, rels, force, schemas, max_pairs, dry_run) -> list:
    cands = join_candidates(rels, schemas)
    pairs = []
    for col, hits in cands.items():
        for (lr, _), (rr, _) in itertools.combinations(hits, 2):
            pairs.append((col, lr, rr))
    print(f"joins: {len(cands)} key-shaped shared column name(s) -> {len(pairs)} raw pairs")
    if dry_run:
        for col, hits in sorted(cands.items(), key=lambda kv: -len(kv[1]))[:12]:
            print(f"    {col:<28} in {len(hits):>3} relations")
        print(f"  (dry run: no queries issued; cap is {max_pairs})")
        return []

    stats: dict = {}
    verified = []
    checked = 0
    for col, lr, rr in pairs:
        if checked >= max_pairs:
            print(f"  reached --max-pairs {max_pairs}; stopping")
            break
        if not (scannable(lr, force) and scannable(rr, force)):
            continue
        ls = await column_stats(backend, stats, lr, col)
        rs = await column_stats(backend, stats, rr, col)
        if not ls or not rs or not ls["non_null"] or not rs["non_null"]:
            continue
        # A real join has a "one" side. If neither side is unique on the key it is
        # a many-to-many fan-out, not a join path worth recommending.
        if not (ls["unique"] or rs["unique"]):
            continue
        left, right = (lr, rr) if rs["unique"] else (rr, lr)
        checked += 1
        try:
            rows = await backend.run(f"""
                WITH l AS (SELECT DISTINCT {col} k FROM {q_ident(left.schema, left.name)}
                           WHERE {col} IS NOT NULL LIMIT 50000),
                     r AS (SELECT DISTINCT {col} k FROM {q_ident(right.schema, right.name)}
                           WHERE {col} IS NOT NULL)
                SELECT count(*) sampled, count(r.k) matched
                FROM l LEFT JOIN r ON l.k = r.k""")
        except Exception as e:
            print(f"  ! {left.addr}.{col} -> {right.addr}.{col}: "
                  f"{str(e).splitlines()[0][:80]}")
            continue
        s, mt = int(rows[0]["SAMPLED"] or 0), int(rows[0]["MATCHED"] or 0)
        if not s:
            continue
        pct = round(100.0 * mt / s, 1)
        card = "many-to-one" if not stats[f"{left.addr}.{col}"]["unique"] else "one-to-one"
        verified.append({"left": left.addr, "right": right.addr, "column": col,
                         "cardinality": card, "match_pct": pct,
                         "measured_on": date.today().isoformat()})
        print(f"  {left.addr}.{col} -> {right.addr}.{col}  {card} {pct}%")
    print(f"  verified {len(verified)} join path(s) from {checked} checked pair(s)")
    return verified


# --------------------------------------------------------------------------
# profiles
# --------------------------------------------------------------------------

async def run_profiles(backend, rels, force, schemas, only, top_n,
                       max_relations=0, done=()) -> dict:
    targets = [r for r in rels if (not schemas or r.schema in schemas)
               and (not only or r.name == only) and r.row_count
               and r.addr not in done]
    # Cheapest first, so a bounded run covers the most relations it can afford.
    targets.sort(key=lambda r: r.bytes or 0)
    if max_relations:
        targets = targets[:max_relations]
    print(f"profiles: {len(targets)} relation(s) to do"
          + (f", {len(done)} already cached" if done else ""))
    out = {}
    for rel in targets:
        if not scannable(rel, force):
            print(f"  skip {rel.addr} ({rel.bytes // 1024**3} GB > scan budget)")
            continue
        sampled = (rel.row_count or 0) > config.PROFILE_SAMPLE_ROWS
        src = (f"(SELECT * FROM {q_ident(rel.schema, rel.name)} "
               f"SAMPLE ({config.PROFILE_SAMPLE_ROWS} ROWS))" if sampled
               else q_ident(rel.schema, rel.name))
        cols = {}
        for c in rel.columns:
            try:
                base = await backend.run(
                    f"SELECT count(*) n, count({c.name}) nn, "
                    f"approx_count_distinct({c.name}) d FROM {src}")
                n, nn, d = (int(base[0]["N"]), int(base[0]["NN"]), int(base[0]["D"]))
                entry = {"nulls_pct": round(100.0 * (n - nn) / n, 1) if n else None,
                         "distinct": d}
                if c.type in config.DATE_TYPES or c.type.startswith(("NUMBER", "FLOAT", "INT")):
                    mm = await backend.run(
                        f"SELECT min({c.name}) lo, max({c.name}) hi FROM {src}")
                    entry["min"], entry["max"] = str(mm[0]["LO"]), str(mm[0]["HI"])
                elif 0 < d <= 5000:
                    tv = await backend.run(
                        f"SELECT {c.name} v, count(*) n FROM {src} "
                        f"WHERE {c.name} IS NOT NULL GROUP BY 1 "
                        f"ORDER BY 2 DESC LIMIT {top_n}")
                    entry["top"] = [{"value": str(r["V"]),
                                     "pct": round(100.0 * int(r["N"]) / nn, 1) if nn else 0}
                                    for r in tv]
                cols[c.name] = entry
            except Exception as e:
                print(f"  ! {rel.addr}.{c.name}: {str(e).splitlines()[0][:70]}")
        out[rel.addr] = {"sampled": sampled, "measured_on": date.today().isoformat(),
                         "columns": cols}
        print(f"  profiled {rel.addr} ({len(cols)} columns{', sampled' if sampled else ''})")
    return out


# --------------------------------------------------------------------------

async def main_async(args) -> int:
    work = Path(args.work)
    facts = load_facts(work, "introspect")
    if not facts:
        raise SystemExit("run introspect.py first")
    rels = relations_from_json(facts["relations"])
    backend = SnowflakeBackend()
    schemas = [s.lower() for s in (args.schema or [])]

    if args.what == "freshness":
        _merge(work, "freshness", await run_freshness(backend, rels, args.force, schemas))
    elif args.what == "joins":
        res = await run_joins(backend, rels, args.force, schemas,
                              args.max_pairs, args.dry_run)
        if not args.dry_run:
            _merge(work, "joins", res)
    elif args.what == "profiles":
        prior = (load_facts(work, "measure") or {}).get("profiles", {})
        fresh = await run_profiles(backend, rels, args.force, schemas, args.relation,
                                   args.top_n, args.max_relations,
                                   done=() if args.redo else tuple(prior))
        _merge(work, "profiles", {**prior, **fresh})
    if not args.dry_run:
        print(f"  -> {work / 'measure.json'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["freshness", "joins", "profiles"])
    ap.add_argument("--work", required=True)
    ap.add_argument("--schema", action="append", help="limit to a schema (repeatable)")
    ap.add_argument("--relation", help="profiles: a single relation name")
    ap.add_argument("--force", action="store_true",
                    help="ignore the per-relation size budget")
    ap.add_argument("--dry-run", action="store_true",
                    help="joins: report candidates without querying")
    ap.add_argument("--max-pairs", type=int, default=config.MAX_JOIN_PAIRS)
    ap.add_argument("--top-n", type=int, default=config.PROFILE_TOP_N)
    ap.add_argument("--max-relations", type=int, default=0,
                    help="profiles: stop after N relations (0 = no limit)")
    ap.add_argument("--redo", action="store_true",
                    help="profiles: re-profile relations already in the work facts")
    args = ap.parse_args()
    if args.what != "joins" and not args.schema:
        args.schema = list(config.INDEX_SCHEMAS)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
