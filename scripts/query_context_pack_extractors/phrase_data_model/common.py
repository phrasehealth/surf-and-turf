"""Shared types, normalization and writers for the Phrase QCP extractor.

Implements the parts of docs/query-context-pack-spec.md that every stage needs:
identifier normalization (§4.1), the `{database}` placeholder (§4.2), evidence
classes (§4.3), timestamps (§4.4), TSV rules (§4.5) and description shaping (§4.6).

Stages communicate through JSON fact files in a work directory rather than calling
each other, so an expensive stage can be re-run without repeating a cheap one.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

QCP_VERSION = "1.0"
DB_PLACEHOLDER = "{database}"

# Evidence classes, ordered as in spec §4.3 (most to least trusted unverified).
EVIDENCE = ("introspected", "declared", "measured", "derived", "inferred", "authored")
WEAK_EVIDENCE = ("inferred", "authored")  # must be visibly marked where read

DESC_MAX = 200


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Identifiers (§4.1)
# --------------------------------------------------------------------------

def normalize(ident: str) -> str:
    """Lowercase, strip surrounding quotes. Used in paths and TSV key fields."""
    s = (ident or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'`":
        s = s[1:-1]
    return s.lower()


def raw_name_for(ident: str) -> str:
    """The exact identifier, kept only when normalization would lose information.

    Snowflake folds unquoted identifiers to upper case, so an all-caps name is
    already lossless under our lowercase normalization; anything else is not.
    """
    s = (ident or "").strip()
    return "" if s == s.upper() else s


def address(schema: str, relation: str, column: str | None = None) -> str:
    a = f"{normalize(schema)}.{normalize(relation)}"
    return f"{a}.{normalize(column)}" if column else a


# --------------------------------------------------------------------------
# Descriptions (§4.6)
# --------------------------------------------------------------------------

def one_line(text: str | None, limit: int = DESC_MAX) -> str:
    """Collapse to a single line, truncate at a word boundary with an ellipsis."""
    s = " ".join((text or "").split())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    if " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,;:.") + "…"


def human_count(n: int | None) -> str:
    if n is None:
        return ""
    if n == 0:
        return "0"
    for div, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            v = n / div
            return f"{v:.2f}".rstrip("0").rstrip(".") + suffix
    return str(n)


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

@dataclass
class Column:
    schema: str
    relation: str
    name: str
    type: str = ""
    nullable: str = ""          # "YES" | "NO" | ""
    ordinal: int = 0
    raw_name: str = ""
    description: str = ""
    description_evidence: str = ""
    evidence: str = "introspected"
    # measured extras
    max_value: str = ""
    profile: dict[str, Any] = field(default_factory=dict)


@dataclass
class Relation:
    schema: str
    name: str
    kind: str = ""              # "base table" | "view" | ...
    raw_name: str = ""
    row_count: int | None = None
    bytes: int | None = None
    last_altered: str = ""
    description: str = ""
    description_full: str = ""
    description_evidence: str = ""
    grain: str = ""
    tags: list[str] = field(default_factory=list)
    evidence: str = "introspected"
    freshness_date: str = ""
    freshness_column: str = ""
    freshness_measured_on: str = ""
    source_path: str = ""
    columns: list[Column] = field(default_factory=list)

    @property
    def addr(self) -> str:
        return address(self.schema, self.name)


# --------------------------------------------------------------------------
# Work-directory fact files
# --------------------------------------------------------------------------

def save_facts(work: Path, stage: str, payload: dict[str, Any]) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    path = work / f"{stage}.json"
    payload = {"stage": stage, "written_at": utcnow(), **payload}
    path.write_text(json.dumps(payload, indent=1, default=str))
    return path


def load_facts(work: Path, stage: str) -> dict[str, Any]:
    path = work / f"{stage}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def relations_to_json(rels: Iterable[Relation]) -> list[dict[str, Any]]:
    return [asdict(r) for r in rels]


def relations_from_json(rows: Iterable[dict[str, Any]]) -> list[Relation]:
    out = []
    for r in rows:
        cols = [Column(**c) for c in r.pop("columns", [])]
        out.append(Relation(columns=cols, **r))
    return out


# --------------------------------------------------------------------------
# TSV writing (§4.5)
# --------------------------------------------------------------------------

_TSV_STRIP = re.compile(r"[\t\r\n]+")


def tsv_field(v: Any) -> str:
    if v is None:
        return ""
    return _TSV_STRIP.sub(" ", str(v)).strip()


def write_tsv(path: Path, header: list[str], rows: Iterable[Iterable[Any]]) -> int:
    """Write a spec-conforming TSV: fixed header, constant field count, sorted."""
    n = len(header)
    body = []
    for row in rows:
        fields = [tsv_field(v) for v in row]
        if len(fields) != n:
            raise ValueError(f"{path.name}: row has {len(fields)} fields, header has {n}")
        body.append("\t".join(fields))
    body.sort()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\t".join(header) + "\n" + "".join(l + "\n" for l in body))
    return len(body)


def mark(evidence: str) -> str:
    """Inline marker for facts the consumer must treat as a hypothesis (§4.3)."""
    return f" ({evidence})" if evidence in WEAK_EVIDENCE else ""
