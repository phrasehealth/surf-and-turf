#!/usr/bin/env python
"""Stage 4 — render a conforming Query Context Pack from the work facts.

    python render.py [--out DIR] [--work DIR]

Merges the stage outputs under the authority rules in spec §7.2: introspection
decides what exists and what its columns are; the manifest supplies meaning;
measurement supplies state and joins. Nothing here queries anything.

`concepts/` is never written by this script, only preserved (§7.3): it is the
one hand-authored part of a pack.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config                                                       # noqa: E402
from common import (DB_PLACEHOLDER, QCP_VERSION, human_count, load_facts,  # noqa: E402
                    mark, one_line, relations_from_json, utcnow, write_tsv)


def _freshness_label(rel, fresh: dict) -> str:
    if rel.row_count == 0:
        return "EMPTY"
    f = fresh.get(rel.addr)
    if not f:
        return ""
    d = f["max"]
    try:
        when = datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        return f"fresh {d}"
    # A max date in the future is not freshness -- the column holds scheduled or
    # projected values, or bad data. Saying "fresh" there would be a lie that
    # reads like a guarantee, so flag it and let the consumer decide.
    if when > date.today():
        return f"FUTURE-DATED {d}"
    if when < date.today() - timedelta(days=config.STALE_AFTER_DAYS):
        return f"STALE {d}"
    return f"fresh {d}"


def render(work: Path, out: Path) -> dict:
    intro = load_facts(work, "introspect")
    if not intro:
        raise SystemExit("run introspect.py first")
    tf = load_facts(work, "transforms") or {}
    ms = load_facts(work, "measure") or {}

    rels = relations_from_json(intro["relations"])
    rel_meta = tf.get("relations", {})
    col_meta = tf.get("columns", {})
    fresh = ms.get("freshness", {})
    joins = ms.get("joins", [])
    profiles = ms.get("profiles", {})
    sources = tf.get("sources", {})

    out.mkdir(parents=True, exist_ok=True)
    stats = defaultdict(int)

    # ---- merge meaning onto the introspected spine -----------------------
    for rel in rels:
        m = rel_meta.get(rel.addr)
        if m:
            rel.description = m["description"]
            rel.description_full = m["description_full"]
            rel.description_evidence = m["description_evidence"]
            rel.grain = m["grain"]
            rel.tags = m["tags"]
        f = fresh.get(rel.addr)
        if f:
            rel.freshness_date, rel.freshness_column = f["max"], f["column"]
            rel.freshness_measured_on = f["measured_on"]
        for c in rel.columns:
            cm = col_meta.get(f"{rel.addr}.{c.name}")
            if cm:
                c.description = cm["description"]
                c.description_evidence = cm["evidence"]
        if rel.description:
            stats["rel_described"] += 1
        stats["col_described"] += sum(1 for c in rel.columns if c.description)

    rels.sort(key=lambda r: (r.schema, r.name))

    # ---- columns.tsv (§5.4) ---------------------------------------------
    n_cols = write_tsv(
        out / "columns.tsv",
        ["schema", "relation", "column", "type", "nullable", "raw_name",
         "evidence", "description"],
        ([r.schema, r.name, c.name, c.type, c.nullable, c.raw_name,
          c.description_evidence or c.evidence, c.description]
         for r in rels for c in sorted(r.columns, key=lambda c: c.ordinal)))

    # ---- joins.tsv (§5.6): both directions, so either lookup finds it ----
    join_rows = []
    for j in joins:
        ls, lr = j["left"].split(".", 1)
        rs, rr = j["right"].split(".", 1)
        rev = {"many-to-one": "one-to-many", "one-to-many": "many-to-one"}.get(
            j["cardinality"], j["cardinality"])
        join_rows.append([ls, lr, j["column"], rs, rr, j["column"],
                          j["cardinality"], j["match_pct"], "measured", j["measured_on"]])
        join_rows.append([rs, rr, j["column"], ls, lr, j["column"],
                          rev, "", "measured", j["measured_on"]])
    n_joins = write_tsv(
        out / "joins.tsv",
        ["left_schema", "left_relation", "left_column", "right_schema",
         "right_relation", "right_column", "cardinality", "match_pct",
         "evidence", "measured_on"], join_rows) if join_rows else 0
    if not join_rows:
        (out / "joins.tsv").unlink(missing_ok=True)

    # ---- lineage.tsv (§5.7) ---------------------------------------------
    lin = tf.get("lineage", [])
    n_lin = write_tsv(
        out / "lineage.tsv",
        ["downstream_schema", "downstream_relation", "downstream_column",
         "upstream_schema", "upstream_relation", "upstream_column",
         "transform", "evidence"], lin) if lin else 0
    if not lin:
        (out / "lineage.tsv").unlink(missing_ok=True)

    # ---- aliases.tsv (§5.8) ---------------------------------------------
    al = tf.get("aliases", [])
    n_al = write_tsv(
        out / "aliases.tsv",
        ["vocabulary", "external_name", "schema", "relation", "column",
         "evidence", "note"], al) if al else 0
    if not al:
        (out / "aliases.tsv").unlink(missing_ok=True)

    # ---- index.md (§5.3) -------------------------------------------------
    by_schema = defaultdict(list)
    for r in rels:
        by_schema[r.schema].append(r)
    idx = ["# Index", "",
           f"One line per relation. Schemas summarised here: "
           f"{', '.join(config.INDEX_SCHEMAS)}. Every other relation still has a page "
           f"and rows in `columns.tsv` — grep for it.", ""]
    indexed = 0
    for schema in sorted(s for s in by_schema if s in config.INDEX_SCHEMAS):
        idx += [f"## {schema}", ""]
        for r in by_schema[schema]:
            parts = [f"- `{r.addr}`"]
            if r.row_count is not None:
                parts.append(f"{human_count(r.row_count)} rows")
            lab = _freshness_label(r, fresh)
            if lab:
                parts.append(lab)
            if r.description:
                parts.append(r.description)
            idx.append(" · ".join(parts))
            indexed += 1
        idx.append("")
    (out / "index.md").write_text("\n".join(idx))

    # ---- relation pages (§5.5) ------------------------------------------
    joins_by_rel = defaultdict(list)
    for row in join_rows:
        joins_by_rel[f"{row[0]}.{row[1]}"].append(row)
    up_by_rel, down_by_rel = defaultdict(set), defaultdict(set)
    for d_s, d_r, d_c, u_s, u_r, u_c, _t, _e in lin:
        if not d_c:
            up_by_rel[f"{d_s}.{d_r}"].add(f"{u_s}.{u_r}")
            down_by_rel[f"{u_s}.{u_r}"].add(f"{d_s}.{d_r}")
    alias_by_rel = defaultdict(list)
    for voc, ext, s, r, _c, ev, _n in al:
        alias_by_rel[f"{s}.{r}"].append((voc, ext, ev))

    for rel in rels:
        p = out / "relations" / rel.schema / f"{rel.name}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        L = [f"# {rel.addr}", ""]
        head = [f"`{DB_PLACEHOLDER}.{rel.addr}`", rel.kind]
        if rel.row_count is not None:
            head.append(f"{rel.row_count:,} rows")
        L.append(" · ".join(head))
        if rel.raw_name:
            L.append(f"raw_name: `{rel.raw_name}`")
        if rel.grain:
            L.append(f"grain: `{rel.grain}`")
        if rel.freshness_date:
            L.append(f"freshness: latest `{rel.freshness_column}` {rel.freshness_date} "
                     f"(measured {rel.freshness_measured_on})")
        elif rel.row_count == 0:
            L.append("freshness: **EMPTY** — this relation has no rows")
        if rel.freshness_date and rel.freshness_date > date.today().isoformat():
            L.append(f"note: `{rel.freshness_column}` holds dates in the future — "
                     f"scheduled/projected values or bad data, not a freshness signal")
        if rel.last_altered:
            L.append(f"last written: {rel.last_altered}")
        if rel.tags:
            L.append(f"tags: {', '.join(rel.tags)}")
        for voc, ext, ev in alias_by_rel.get(rel.addr, []):
            L.append(f"alias: {voc} `{ext}`{mark(ev)}")
        L.append("")
        if rel.description_full:
            L += [rel.description_full, ""]

        L += ["## Columns", "", "| column | type | null | description |", "|---|---|---|---|"]
        for c in sorted(rel.columns, key=lambda c: c.ordinal):
            L.append(f"| `{c.name}` | {c.type} | {c.nullable} | {c.description} |")
        L.append("")

        jr = joins_by_rel.get(rel.addr, [])
        if jr:
            L += ["## Joins", ""]
            for row in sorted(jr):
                pct = f" · {row[7]}% matched" if row[7] != "" else ""
                L.append(f"- `{row[2]}` → `{row[3]}.{row[4]}.{row[5]}` · {row[6]}"
                         f"{pct} · measured {row[9]}")
            L.append("")
        ups, downs = sorted(up_by_rel.get(rel.addr, ())), sorted(down_by_rel.get(rel.addr, ()))
        if ups or downs:
            L += ["## Lineage", ""]
            if ups:
                L.append("upstream: " + ", ".join(f"`{u}`" for u in ups))
            if downs:
                L.append("downstream: " + ", ".join(f"`{d}`" for d in downs))
            L.append("")
        if rel.addr in profiles:
            L += [f"## Profile", "",
                  f"`profiles/{rel.schema}/{rel.name}.md` — value distributions", ""]
        if rel.addr in sources:
            L += ["## Definition", "",
                  f"`sources/{rel.schema}/{rel.name}.sql` — transform source, "
                  f"read for computation questions only", ""]
        p.write_text("\n".join(L))
        stats["pages"] += 1

    # ---- sources (§5.11) -------------------------------------------------
    for addr, sql in sources.items():
        s, r = addr.split(".", 1)
        sp = out / "sources" / s / f"{r}.sql"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(sql)
        stats["sources"] += 1

    # ---- profiles (§5.9) -------------------------------------------------
    for addr, prof in profiles.items():
        s, r = addr.split(".", 1)
        pp = out / "profiles" / s / f"{r}.md"
        pp.parent.mkdir(parents=True, exist_ok=True)
        P = [f"# Profile — {addr}", "",
             f"measured_on: {prof['measured_on']} · rows sampled: "
             f"{'sample' if prof['sampled'] else 'full'}", "",
             "| column | nulls | distinct | values |", "|---|---|---|---|"]
        for cname, e in prof["columns"].items():
            if "top" in e:
                vals = ", ".join(f"`{t['value']}` ({t['pct']}%)" for t in e["top"])
            elif "min" in e:
                vals = f"min {e['min']}, max {e['max']}"
            else:
                vals = ""
            P.append(f"| `{cname}` | {e.get('nulls_pct', '')}% | {e.get('distinct', '')} | {vals} |")
        pp.write_text("\n".join(P) + "\n")
        stats["profiles"] += 1

    # ---- conformance (§6) ------------------------------------------------
    level = "L0"
    if stats["rel_described"]:
        level = "L1"
    if n_lin or n_al:
        level = "L2"
    if any(r.row_count is not None for r in rels) and fresh:
        level = "L3"
    if n_joins and profiles:
        level = "L4"

    disc = tf.get("discrepancies", {})
    n_rel, n_col = len(rels), sum(len(r.columns) for r in rels)
    manifest = [
        f"# Query Context Pack — {intro.get('database_introspected', 'unknown')}", "",
        f"qcp_version: {QCP_VERSION}",
        f"pack_name: {intro.get('database_introspected', 'unknown')}",
        f"built_at: {utcnow()}",
        f"database_placeholder: {DB_PLACEHOLDER}",
        f"schemas: {', '.join(sorted(by_schema))}",
        f"relations: {n_rel}",
        f"columns: {n_col}",
        f"conformance: {level}",
        f"index_schemas: {', '.join(config.INDEX_SCHEMAS)}",
        f"stale_after_days: {config.STALE_AFTER_DAYS}",
        "evidence_present: " + ", ".join(
            e for e, present in (
                ("introspected", True), ("declared", bool(stats["rel_described"])),
                ("derived", bool(n_lin or n_al)), ("measured", bool(fresh or joins)),
                ("authored", any((out / "concepts").glob("*.md")))) if present),
        "sources:",
        f"  - introspection: {intro.get('source', '')} @ {intro.get('introspected_at', '')}",
    ]
    if tf:
        manifest.append(f"  - transforms: {tf.get('manifest', '')} @ {tf.get('read_at', '')}")
    if ms:
        manifest.append(f"  - measurement: qcp measure @ {ms.get('written_at', '')}")
    manifest += [
        "coverage:",
        f"  descriptions: {stats['rel_described']}/{n_rel} relations, "
        f"{stats['col_described']}/{n_col} columns",
        f"  indexed: {indexed}/{n_rel} relations summarised in index.md",
        f"  freshness: {len(fresh)}/{n_rel} relations measured",
        f"  joins: {n_joins // 2 if n_joins else 0} verified, 0 inferred",
        f"  profiles: {len(profiles)} relations",
        f"  empty_relations: {sum(1 for r in rels if r.row_count == 0)}",
        "discrepancies:",
        f"  models_without_relation: {len(disc.get('models_without_relation', []))}",
        f"  relations_without_model: {len(disc.get('relations_without_model', []))}",
    ]
    (out / "MANIFEST.md").write_text("\n".join(manifest) + "\n")

    write_readme(out, level, intro, by_schema)
    (out / "concepts").mkdir(exist_ok=True)   # preserved, never written here

    return {"relations": n_rel, "columns": n_cols, "pages": stats["pages"],
            "joins": n_joins, "lineage": n_lin, "aliases": n_al,
            "sources": stats["sources"], "profiles": stats["profiles"],
            "level": level, "indexed": indexed}


def write_readme(out: Path, level: str, intro: dict, by_schema) -> None:
    has = lambda n: (out / n).exists()
    steps = [
        "1. A term that looks like a relation → search `index.md`.",
        "2. A term that is not there → `Grep` "
        + ("`aliases.tsv`, then " if has("aliases.tsv") else "") + "`columns.tsv`. "
        "Users name columns and source-system objects at least as often as tables.",
        "3. A chosen relation → `Read relations/<schema>/<relation>.md`. **This step is "
        "not optional.** `index.md` carries no column names, so finding your relation "
        "there does not equip you to write a query — the page does.",
    ]
    if has("joins.tsv"):
        steps.append("4. A second relation → `Grep joins.tsv` for both addresses **before "
                     "writing any JOIN**. No row means the join is unverified: say so, "
                     "and prefer a single-relation answer or ask.")
    else:
        steps.append("4. A second relation → this pack has no verified joins. Any join is "
                     "your hypothesis; state it as one.")
    if has("profiles"):
        steps.append("5. A filter on an unfamiliar column → `Read profiles/<schema>/"
                     "<relation>.md` if present; otherwise `SELECT DISTINCT` a sample first.")
    if has("lineage.tsv"):
        steps.append("6. \"Where does this come from / end up\" → `Grep lineage.tsv` for the "
                     "address. It holds both relation-level and column-level edges.")
    steps.append("7. How a value is computed → the `Definition` path on the relation page. "
                 "Last resort.")
    steps.append("8. A domain term that is not a relation or column → `Glob concepts/*.md`.")

    (out / "README.md").write_text(f"""# Query Context Pack — how to use this

This directory describes one database for an agent that writes queries against it.
It conforms to Query Context Pack {QCP_VERSION}, conformance level **{level}**.
Read `MANIFEST.md` for what it covers and where it is thin.

## Standing context

Load `MANIFEST.md` and `index.md` only. Everything else is on demand. Do not load
`columns.tsv`, a profile, or the whole `relations/` tree into context — they are
grep and glob targets.

## Lookup protocol

{chr(10).join(steps)}

## Fully-qualified names

Relations are written `{DB_PLACEHOLDER}.<schema>.<relation>`. Substitute your own
connection's database for `{DB_PLACEHOLDER}`. The database is never stated by this
pack, because the same shape is deployed to more than one.

Schemas present: {', '.join(sorted(by_schema))}.

## Trust

Every fact carries an evidence class. Treat `introspected`, `declared`, `derived`
and `measured` as reliable. Treat `inferred` and `authored` as hypotheses — verify
before relying on them, and say in your answer that they were assumptions.

If `built_at` in `MANIFEST.md` is old, confirm relation and column existence against
the live database before your final query. **Never invent a column**: if it is not in
this pack and not in a live `DESCRIBE`, say so rather than guessing a plausible name.

**The column gate.** Before writing a column name in a query you must have read that
exact name, for that exact relation, in `relations/<schema>/<relation>.md`, in
`columns.tsv`, or from a live `DESCRIBE`. Plausibility is not evidence: a relation
whose description says "diagnosis reference" may well call the column `dx_code` and
not `code`. Reaching for the likely spelling costs a failed query and a turn.

## Reporting obligation

When your answer is a number someone will act on, name the relations you used and
pass through any `EMPTY`, `STALE` or `FUTURE-DATED` marker on them. A freshness caveat this pack
supplied and your answer dropped is the most damaging way this system fails: a
confidently wrong number that nothing downstream catches.
""")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    res = render(Path(args.work), Path(args.out))
    print(f"rendered {args.out}")
    for k, v in res.items():
        print(f"  {k:<12} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
