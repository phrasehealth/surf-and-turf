#!/usr/bin/env python
"""Stage 2 — dbt manifest. Adds meaning, lineage, aliases and definition sources.

    python transforms.py [--project epic] [--work DIR]

Per spec §7.2 this stage is authoritative for *meaning* (descriptions, grain,
tags) and contributes lineage, but it is never allowed to assert what exists.
Models are bound to introspected relations by identifier alone; the manifest's
own `database`/`schema`/`relation_name` are discarded, because they record the
target that happened to compile it, not where the relation lives (§7.2, §7.4).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config                                                    # noqa: E402
from common import (load_facts, normalize, one_line, save_facts,  # noqa: E402
                    relations_from_json, utcnow)

# `-- Epic ORDER_PROC table with Snowpipe auto-ingestion`  → external vocabulary name
ALIAS_HEADER = re.compile(r"--\s*(?:Epic|Cerner|Millennium)\s+([A-Za-z0-9_]+)\s+table", re.I)
# A column description that names its own source: "(alert_burden_index.thirty_day_index)"
COL_SOURCE = re.compile(r"\(([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\)")


def load_models(manifest_path: Path) -> dict[str, dict]:
    if not manifest_path.exists():
        raise SystemExit(
            f"manifest not found: {manifest_path}\n"
            f"  build it with:  cd {manifest_path.parents[1]} && make parse\n"
            f"  (or: uv run dbt parse --target <org>_gold --profiles-dir ../..)")
    m = json.loads(manifest_path.read_text())
    return {uid: n for uid, n in m.get("nodes", {}).items()
            if n.get("resource_type") == "model"}


def bind(models: dict[str, dict], introspected: list) -> tuple[dict[str, str], dict]:
    """Map each dbt model unique_id to an introspected `schema.relation` address.

    Matching is by identifier. Where a name exists in more than one schema the
    model's layer directory is used only to break the tie, and the choice is
    recorded — a hint, never an authority (spec §7.4).
    """
    by_name: dict[str, list[str]] = defaultdict(list)
    for r in introspected:
        by_name[r.name].append(r.addr)

    bound: dict[str, str] = {}
    report = {"bound": 0, "tie_broken": 0, "unmatched": [], "ambiguous_unresolved": []}
    for uid, n in models.items():
        name = normalize(n.get("alias") or n.get("name"))
        hits = by_name.get(name, [])
        if len(hits) == 1:
            bound[uid] = hits[0]
            report["bound"] += 1
        elif len(hits) > 1:
            # path is models/<pkg>/<layer>/... — use <layer> only as a tiebreaker
            parts = (n.get("path") or "").split("/")
            layer = parts[1] if len(parts) > 1 else ""
            pick = next((h for h in hits if h.split(".")[0] == layer), None)
            if pick:
                bound[uid] = pick
                report["tie_broken"] += 1
            else:
                report["ambiguous_unresolved"].append({"model": name, "candidates": hits})
        else:
            report["unmatched"].append(name)
    return bound, report


def extract(models: dict[str, dict], bound: dict[str, str], vocabulary: str) -> dict:
    addr_of = bound
    rel_meta: dict[str, dict] = {}
    col_meta: dict[str, dict] = {}
    lineage: list[list[str]] = []
    aliases: list[list[str]] = []
    sources: dict[str, str] = {}

    for uid, n in models.items():
        addr = addr_of.get(uid)
        if not addr:
            continue
        schema, relation = addr.split(".", 1)
        desc_full = " ".join((n.get("description") or "").split())
        pk = n.get("primary_key") or []
        rel_meta[addr] = {
            "description": one_line(desc_full),
            "description_full": desc_full,
            "description_evidence": "declared" if desc_full else "",
            "grain": ", ".join(normalize(c) for c in pk),
            "tags": sorted(t for t in (n.get("tags") or []) if not t.startswith("pr_")),
            "dbt_path": n.get("original_file_path") or "",
        }

        for cname, cmeta in (n.get("columns") or {}).items():
            cdesc = " ".join((cmeta.get("description") or "").split())
            if not cdesc:
                continue
            key = f"{addr}.{normalize(cname)}"
            col_meta[key] = {"description": one_line(cdesc), "evidence": "declared"}
            # Column-level provenance, where a description states its own source.
            # Declared by a human in schema.yml, so `declared` — never `inferred`.
            for up_rel, up_col in COL_SOURCE.findall(cdesc):
                up_addr = next((a for u, a in addr_of.items()
                                if a.split(".", 1)[1] == up_rel), None)
                if up_addr:
                    us, ur = up_addr.split(".", 1)
                    lineage.append([schema, relation, normalize(cname),
                                    us, ur, normalize(up_col), "derive", "declared"])

        # Relation-level lineage from the dbt DAG.
        for dep in (n.get("depends_on") or {}).get("nodes", []):
            up = addr_of.get(dep)
            if up:
                us, ur = up.split(".", 1)
                lineage.append([schema, relation, "", us, ur, "", "", "derived"])

        raw = n.get("raw_code") or ""
        if raw:
            sources[addr] = raw
        m = ALIAS_HEADER.search(raw)
        if m:
            aliases.append([vocabulary, m.group(1).upper(), schema, relation, "",
                            "derived", "ingest passthrough; header-declared source table"])

    return {"relations": rel_meta, "columns": col_meta, "lineage": lineage,
            "aliases": aliases, "sources": sources}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default="epic", choices=sorted(config.PROJECTS))
    ap.add_argument("--work", required=True)
    args = ap.parse_args()
    work = Path(args.work)

    facts = load_facts(work, "introspect")
    if not facts:
        raise SystemExit("run introspect.py first")
    introspected = relations_from_json(facts["relations"])

    _, manifest_path = config.project_paths(args.project)
    models = load_models(manifest_path)
    vocabulary = config.PROJECTS[args.project]["vocabulary"]
    print(f"manifest: {manifest_path}")
    print(f"  {len(models)} models")

    bound, report = bind(models, introspected)
    print(f"  bound {report['bound']} by name, {report['tie_broken']} by layer tiebreak")
    if report["unmatched"]:
        print(f"  ! {len(report['unmatched'])} model(s) have no relation in the warehouse")
    if report["ambiguous_unresolved"]:
        print(f"  ! {len(report['ambiguous_unresolved'])} ambiguous, unresolved")

    bound_addrs = set(bound.values())
    undescribed = [r.addr for r in introspected if r.addr not in bound_addrs]
    if undescribed:
        print(f"  ! {len(undescribed)} warehouse relation(s) have no dbt model")

    data = extract(models, bound, vocabulary)
    print(f"  descriptions: {sum(1 for v in data['relations'].values() if v['description'])} "
          f"relations, {len(data['columns'])} columns")
    print(f"  lineage: {sum(1 for l in data['lineage'] if not l[2])} relation-level, "
          f"{sum(1 for l in data['lineage'] if l[2])} column-level")
    print(f"  aliases: {len(data['aliases'])} ({vocabulary})")

    save_facts(work, "transforms", {
        "project": args.project, "manifest": str(manifest_path),
        "vocabulary": vocabulary, "read_at": utcnow(),
        "discrepancies": {
            "models_without_relation": sorted(report["unmatched"]),
            "relations_without_model": sorted(undescribed),
            "ambiguous": report["ambiguous_unresolved"],
        },
        **data,
    })
    print(f"  -> {work / 'transforms.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
