#!/usr/bin/env python
"""Build a Query Context Pack for the Phrase data model, end to end.

    python build.py --database penn                 # L2, no table is scanned
    python build.py --database penn --freshness     # L3
    python build.py --database penn --joins --profiles
    python build.py --database penn --stage render  # re-render, no queries

`--database` is required: a pack describes one database, and it decides where the
pack lands. Each conversation's workspace is that directory, so a tenant's schema
cannot leak into another's.

Stages are ordered cheapest-first (spec §7.5) so a useful pack exists before any
scanning starts, and each writes its facts to the work directory, so re-running
a later stage never repeats an earlier one.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config  # noqa: E402

PY = sys.executable


def run(script: str, *args: str) -> None:
    cmd = [PY, str(HERE / script), *args]
    print(f"\n$ {' '.join(a.replace(str(HERE) + '/', '') for a in cmd[1:])}")
    r = subprocess.run(cmd)
    if r.returncode:
        raise SystemExit(f"{script} failed with {r.returncode}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Required: a pack describes one database, and which one is not guessable.
    # It also decides where the pack lands, since the agent's cwd is that directory.
    ap.add_argument("--database", required=True,
                    help="the database to describe, e.g. penn")
    ap.add_argument("--project", default="epic", choices=sorted(config.PROJECTS))
    ap.add_argument("--work", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--stage", action="append",
                    choices=["introspect", "transforms", "render", "validate"],
                    help="run only these cheap stages (repeatable)")
    ap.add_argument("--freshness", action="store_true", help="measure latest business date")
    ap.add_argument("--joins", action="store_true", help="measure join paths (costly)")
    ap.add_argument("--profiles", action="store_true", help="measure value profiles (costly)")
    ap.add_argument("--force", action="store_true", help="ignore per-relation size budget")
    args = ap.parse_args()

    stages = args.stage or ["introspect", "transforms", "render", "validate"]
    work = args.work or str(config.work_dir(args.database))
    out = args.out or str(config.out_dir(args.database))
    w, o = ["--work", work], ["--out", out]
    db = ["--database", args.database]

    if "introspect" in stages:
        run("introspect.py", *w, *db)
    if "transforms" in stages:
        run("transforms.py", "--project", args.project, *w)
    force = ["--force"] if args.force else []
    if args.freshness:
        run("measure.py", "freshness", *w, *force)
    if args.joins:
        run("measure.py", "joins", *w, *force)
    if args.profiles:
        run("measure.py", "profiles", *w, *force)
    if "render" in stages:
        run("render.py", *w, *o)
    if "validate" in stages:
        run("validate.py", "--pack", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
