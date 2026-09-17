#!/usr/bin/env python
"""Conformance validator for a Query Context Pack (spec §9).

    python validate.py [--pack DIR]

Exits non-zero on any failure. Warnings are reported but do not fail the run:
they describe a pack that is thin, not one that is malformed.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

OK, BAD, WARN = "\033[32m✓\033[0m", "\033[31m✗\033[0m", "\033[33m!\033[0m"

REQUIRED = ["README.md", "MANIFEST.md", "index.md", "columns.tsv"]
HEADERS = {
    "columns.tsv": ["schema", "relation", "column", "type", "nullable", "raw_name",
                    "evidence", "description"],
    "joins.tsv": ["left_schema", "left_relation", "left_column", "right_schema",
                  "right_relation", "right_column", "cardinality", "match_pct",
                  "evidence", "measured_on"],
    "lineage.tsv": ["downstream_schema", "downstream_relation", "downstream_column",
                    "upstream_schema", "upstream_relation", "upstream_column",
                    "transform", "evidence"],
    "aliases.tsv": ["vocabulary", "external_name", "schema", "relation", "column",
                    "evidence", "note"],
}
EVIDENCE = {"introspected", "declared", "measured", "derived", "inferred", "authored"}


class Report:
    def __init__(self) -> None:
        self.fail: list[str] = []
        self.warn: list[str] = []

    def check(self, cond: bool, msg: str) -> bool:
        if not cond:
            self.fail.append(msg)
        return cond

    def caution(self, cond: bool, msg: str) -> None:
        if not cond:
            self.warn.append(msg)


def read_tsv(path: Path) -> tuple[list[str], list[list[str]]]:
    lines = path.read_text().splitlines()
    if not lines:
        return [], []
    return lines[0].split("\t"), [l.split("\t") for l in lines[1:]]


def validate(pack: Path) -> Report:
    r = Report()

    # --- structural ---
    for name in REQUIRED:
        r.check((pack / name).exists(), f"missing required file {name}")
    if r.fail:
        return r

    man = (pack / "MANIFEST.md").read_text()
    meta = dict(re.findall(r"^(\w+):\s*(.+)$", man, re.M))
    r.check("qcp_version" in meta, "MANIFEST.md has no qcp_version")
    r.check("conformance" in meta, "MANIFEST.md has no conformance level")

    for name, header in HEADERS.items():
        p = pack / name
        if not p.exists():
            continue
        got, rows = read_tsv(p)
        r.check(got == header, f"{name}: header is {got}, expected {header}")
        bad = [i for i, row in enumerate(rows, 2) if len(row) != len(header)]
        r.check(not bad, f"{name}: {len(bad)} line(s) have the wrong field count "
                         f"(first at line {bad[0] if bad else '-'})")

    # --- relation pages exist for every relation in columns.tsv, and vice versa ---
    _, crows = read_tsv(pack / "columns.tsv")
    addrs = {(row[0], row[1]) for row in crows if len(row) > 1}
    missing = [f"{s}.{t}" for s, t in addrs if not (pack / "relations" / s / f"{t}.md").exists()]
    r.check(not missing, f"{len(missing)} relation(s) in columns.tsv have no page "
                         f"(e.g. {missing[:3]})")
    pages = {(p.parent.name, p.stem) for p in (pack / "relations").rglob("*.md")}
    orphan = pages - addrs
    r.check(not orphan, f"{len(orphan)} page(s) have no columns.tsv rows (e.g. "
                        f"{sorted(orphan)[:3]})")

    # --- no tabs/newlines inside fields; no wrapped facts ---
    for name in HEADERS:
        p = pack / name
        if p.exists():
            r.check("\t\t\t\t\t\t\t\t\t\t\t" not in p.read_text(),
                    f"{name}: suspicious run of empty fields")

    # --- referential ---
    def resolves(s: str, t: str) -> bool:
        return not t or (s, t) in addrs

    for name, (li, ri) in {"joins.tsv": ((0, 1), (3, 4)),
                           "lineage.tsv": ((0, 1), (3, 4))}.items():
        p = pack / name
        if not p.exists():
            continue
        _, rows = read_tsv(p)
        bad = [row for row in rows
               if not resolves(row[li[0]], row[li[1]]) or not resolves(row[ri[0]], row[ri[1]])]
        r.check(not bad, f"{name}: {len(bad)} row(s) reference an unknown relation "
                         f"(e.g. {bad[0][:5] if bad else ''})")

    p = pack / "aliases.tsv"
    if p.exists():
        _, rows = read_tsv(p)
        bad = [row for row in rows if row[3] and not resolves(row[2], row[3])]
        r.check(not bad, f"aliases.tsv: {len(bad)} row(s) reference an unknown relation")

    # --- semantic ---
    for name, idx in {"columns.tsv": 6, "joins.tsv": 8, "lineage.tsv": 7,
                      "aliases.tsv": 5}.items():
        p = pack / name
        if not p.exists():
            continue
        _, rows = read_tsv(p)
        bad = {row[idx] for row in rows if row[idx] and row[idx] not in EVIDENCE}
        r.check(not bad, f"{name}: unknown evidence class(es) {sorted(bad)}")

    p = pack / "joins.tsv"
    if p.exists():
        _, rows = read_tsv(p)
        r.check(all(row[9] for row in rows if row[8] == "measured"),
                "joins.tsv: a measured row has no measured_on")

    # no concrete database where the placeholder is required
    leaked = []
    for page in (pack / "relations").rglob("*.md"):
        head = page.read_text().split("\n", 4)[2] if page.read_text().count("\n") > 2 else ""
        if head.startswith("`") and "{database}" not in head:
            leaked.append(page.name)
    r.check(not leaked, f"{len(leaked)} page(s) omit the {{database}} placeholder "
                        f"(e.g. {leaked[:3]})")

    # concepts must declare owner + review date
    for c in (pack / "concepts").glob("*.md") if (pack / "concepts").exists() else []:
        txt = c.read_text()
        r.check("evidence: authored" in txt, f"concepts/{c.name}: no `evidence: authored`")
        r.check("owner:" in txt, f"concepts/{c.name}: no owner")
        m = re.search(r"reviewed:\s*(\d{4}-\d{2}-\d{2})", txt)
        r.check(bool(m), f"concepts/{c.name}: no review date")
        if m:
            reviewed = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            r.caution(reviewed > date.today() - timedelta(days=180),
                      f"concepts/{c.name}: last reviewed {m.group(1)}")

    # --- health warnings ---
    idx = (pack / "index.md").read_text()
    # A description is the last ` · ` segment, and is never a rows/freshness token.
    tokenish = re.compile(r"^(\d[\d.]*[KMB]? rows|fresh \d{4}-\d\d-\d\d|"
                          r"STALE \d{4}-\d\d-\d\d|EMPTY)$")
    undesc = [l for l in idx.splitlines() if l.startswith("- `")
              and tokenish.match(l.rsplit(" · ", 1)[-1].strip())]
    r.caution(not undesc, f"{len(undesc)} indexed relation(s) have no description")
    budget = 60_000
    r.caution(len(idx) <= budget,
              f"index.md is {len(idx) // 1024} KB, over the {budget // 1024} KB "
              f"standing-context budget")
    for key in ("models_without_relation", "relations_without_model"):
        m = re.search(rf"{key}:\s*(\d+)", man)
        if m and int(m.group(1)):
            r.warn.append(f"{key}: {m.group(1)} (introspection/transform discrepancy)")
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", default=str(config.DEFAULT_OUT))
    args = ap.parse_args()
    pack = Path(args.pack)
    if not pack.exists():
        raise SystemExit(f"no pack at {pack}")
    print(f"validating {pack}")
    r = validate(pack)
    for w in r.warn:
        print(f"  {WARN} {w}")
    for f in r.fail:
        print(f"  {BAD} {f}")
    if r.fail:
        print(f"\n{BAD} {len(r.fail)} failure(s)")
        return 1
    print(f"\n{OK} conforming pack ({len(r.warn)} warning(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
