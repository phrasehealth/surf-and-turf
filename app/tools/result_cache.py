"""Recent `run_sql` results, so an analysis can adopt one instead of re-running it.

`record_analysis` binds a specification to results the agent has already produced
(docs/persistence-schema.md §7, "Adopt, do not execute"). The server does not
otherwise keep them: `run_sql` returns rows to the model and discards them.

Keyed by SQL rather than by `tool_use_id`, because an MCP tool handler receives its
arguments and nothing else — it never learns its own id. `AgentSession` sees both on
the event stream, so it binds one to the other as the turn runs.

One cache per conversation, bounded two ways: `max_entries` by count and `ttl_s` by
age. Each entry is already small — `run_sql` caps results at SQL_ROW_LIMIT rows.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

MAX_ENTRIES = 32
TTL_S = 30 * 60


def digest(sql: str) -> str:
    """A key that ignores whitespace, so the agent may reformat what it quotes back."""
    return hashlib.sha256(" ".join((sql or "").split()).encode()).hexdigest()[:16]


@dataclass
class CachedResult:
    sql: str
    rows: list[dict[str, Any]]
    duration_ms: int
    error: str | None
    at: float = field(default_factory=time.time)

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass
class ResultCache:
    max_entries: int = MAX_ENTRIES
    ttl_s: float = TTL_S
    _results: "OrderedDict[str, CachedResult]" = field(default_factory=OrderedDict)
    _by_tool_use: dict[str, str] = field(default_factory=dict)

    # -- writing -----------------------------------------------------------

    def put(self, sql: str, rows: list[dict[str, Any]], duration_ms: int,
            error: str | None = None, aliases: list[str] | None = None) -> str:
        """Store a result under `sql`, and under any `aliases` that name it too.

        The guard rewrites what it executes — appending a LIMIT — so the SQL the
        agent sent and the SQL that ran are different strings for the same result.
        Both have to find it: the agent quotes the former, the log holds the latter.
        """
        key = digest(sql)
        entry = CachedResult(sql, rows, duration_ms, error)
        self._results[key] = entry
        self._results.move_to_end(key)
        for alias in aliases or []:
            akey = digest(alias)
            if akey != key:
                self._results[akey] = entry
                self._results.move_to_end(akey)
        self._evict()
        return key

    def bind(self, tool_use_id: str, sql: str) -> None:
        """Associate a tool call with the SQL it ran, seen on the event stream."""
        if tool_use_id and sql:
            self._by_tool_use[tool_use_id] = digest(sql)

    # -- reading -----------------------------------------------------------

    def get(self, tool_use_id: str) -> CachedResult | None:
        key = self._by_tool_use.get(tool_use_id)
        return self._get_key(key) if key else None

    def get_by_sql(self, sql: str) -> CachedResult | None:
        """Fallback when a tool_use_id is unknown but the SQL is quoted verbatim."""
        return self._get_key(digest(sql))

    def _get_key(self, key: str) -> CachedResult | None:
        entry = self._results.get(key)
        if entry is None:
            return None
        if time.time() - entry.at > self.ttl_s:
            self._results.pop(key, None)
            return None
        self._results.move_to_end(key)
        return entry

    def _evict(self) -> None:
        cutoff = time.time() - self.ttl_s
        for key in [k for k, v in self._results.items() if v.at < cutoff]:
            self._results.pop(key, None)
        while len(self._results) > self.max_entries:
            self._results.popitem(last=False)          # oldest use first

    def __len__(self) -> int:
        return len(self._results)


# Parameters are substituted as :name; this finds them in a template.
_PARAM = re.compile(r"(?<![:\w]):([a-zA-Z_][a-zA-Z0-9_]*)")


def template_params(sql_template: str) -> set[str]:
    return set(_PARAM.findall(sql_template or ""))


def bind_template(sql_template: str, params: dict[str, Any]) -> str:
    """Substitute :name bindings, for comparing a template against what ran.

    Deliberately crude — this is used to check how closely a template reproduces the
    executed SQL, not to build SQL for execution. Templates are allowed to
    approximate (§7); the comparison is recorded, not enforced.
    """
    def literal(v: Any) -> str:
        if isinstance(v, (list, tuple, set)):
            return ", ".join(literal(x) for x in v)
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"

    out = sql_template or ""
    for name, value in sorted(params.items(), key=lambda kv: -len(kv[0])):
        out = re.sub(rf"(?<![:\w]):{re.escape(name)}\b", lambda _m, v=value: literal(v), out)
    return out


def matches(sql_template: str, params: dict[str, Any], executed: str) -> bool:
    """Whether binding the template reproduces the executed SQL, ignoring whitespace."""
    return digest(bind_template(sql_template, params)) == digest(executed)
