"""Turn a recorded chart spec and a query result into SVG.

The model never writes SVG and never names a colour. It declares a `chart_type` and
which result columns fill which channel; everything visual — palette, geometry, axis
ticks, label truncation — comes from `app/charts.py`. A chart therefore cannot drift
from the house style, and cannot disagree with the numbers, because it is drawn from
the same rows the analysis adopted.

Figure numbering and the caption are not here: a figure is captioned by its position
in the report, which is not known until publish (docs/persistence-schema.md §3.2).
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from . import charts

log = logging.getLogger("report-agent.charts")

MAX_CATEGORIES = 24          # beyond this a bar chart is a wall, not a chart


class ChartError(ValueError):
    """The spec and the result do not fit together. Reported, never guessed around."""


def _column(rows: list[dict[str, Any]], name: str, what: str) -> list[Any]:
    if not name:
        raise ChartError(f"chart_spec is missing `{what}`")
    # Result keys come back upper-cased from Snowflake; be forgiving about case.
    lookup = {str(k).lower(): k for k in rows[0]}
    key = lookup.get(str(name).lower())
    if key is None:
        raise ChartError(
            f"chart_spec.{what} names column {name!r}, which the query did not "
            f"return. Available: {', '.join(sorted(rows[0]))}")
    return [r[key] for r in rows]


def _numeric(values: list[Any], what: str) -> list[float]:
    out = []
    for v in values:
        try:
            out.append(float(v if v is not None else 0))
        except (TypeError, ValueError):
            raise ChartError(f"chart_spec.{what} must be numeric; got {v!r}")
    return out


def _pairs(rows, spec, label_key: str, value_key: str) -> list[tuple[str, float]]:
    labels = _column(rows, spec.get(label_key), label_key)
    values = _numeric(_column(rows, spec.get(value_key), value_key), value_key)
    return [(str(a if a is not None else "—"), b) for a, b in zip(labels, values)]


# --------------------------------------------------------------------- forms

def _hbar(rows, spec) -> str:
    pairs = _pairs(rows, spec, "category", "value")
    # Largest first unless the spec asks otherwise: a ranked bar chart is read by
    # length, and an arbitrary order makes the reader do the sorting.
    if spec.get("sort", "desc") == "desc":
        pairs.sort(key=lambda p: -p[1])
    return charts.hbar(pairs[:MAX_CATEGORIES], title="")


def _vbar(rows, spec) -> str:
    pairs = _pairs(rows, spec, "x", "value")
    return charts.vbar(pairs[:MAX_CATEGORIES], title="",
                       axis_label=str(spec.get("axis_label") or ""))


def _line(rows, spec) -> str:
    pairs = _pairs(rows, spec, "x", "value")
    return charts.line(pairs, title="")


def _lines(rows, spec) -> str:
    """One line per series: x, series and value columns."""
    xs = [str(v) for v in _column(rows, spec.get("x"), "x")]
    names = [str(v) for v in _column(rows, spec.get("series"), "series")]
    values = _numeric(_column(rows, spec.get("value"), "value"), "value")

    x_labels = list(dict.fromkeys(xs))                 # first-seen order
    index = {x: i for i, x in enumerate(x_labels)}
    series: dict[str, list[float | None]] = {}
    for x, name, v in zip(xs, names, values):
        series.setdefault(name, [None] * len(x_labels))[index[x]] = v
    return charts.lines(x_labels, series, title="")


def _stacked(rows, spec) -> str:
    """One bar per group, segmented by category."""
    groups = [str(v) for v in _column(rows, spec.get("x"), "x")]
    cats = [str(v) for v in _column(rows, spec.get("series"), "series")]
    values = _numeric(_column(rows, spec.get("value"), "value"), "value")

    order = list(dict.fromkeys(groups))
    categories = list(dict.fromkeys(cats))
    table = {g: {c: 0.0 for c in categories} for g in order}
    for g, c, v in zip(groups, cats, values):
        table[g][c] += v
    return charts.stacked([(g, table[g]) for g in order], categories, title="")


def _stat_tiles(rows, spec) -> str:
    """One tile per row: a label and a figure."""
    labels = _column(rows, spec.get("category"), "category")
    values = _column(rows, spec.get("value"), "value")
    tiles = [(str(a), f"{float(b):,.0f}" if isinstance(b, (int, float)) else str(b))
             for a, b in zip(labels, values)][:6]
    return charts.stat_tiles(tiles)


FORMS: dict[str, Callable[[list[dict[str, Any]], dict[str, Any]], str]] = {
    "hbar": _hbar, "vbar": _vbar, "line": _line, "lines": _lines,
    "stacked": _stacked, "stat_tiles": _stat_tiles,
}

# What each form needs in chart_spec, for the tool description and for errors.
CHANNELS = {
    "hbar": ("category", "value"),
    "vbar": ("x", "value"),
    "line": ("x", "value"),
    "lines": ("x", "series", "value"),
    "stacked": ("x", "series", "value"),
    "stat_tiles": ("category", "value"),
}


def render(chart_type: str, spec: dict[str, Any] | None,
           rows: list[dict[str, Any]]) -> str:
    """SVG for one chart, or raise ChartError saying exactly what does not fit."""
    form = FORMS.get(str(chart_type or "").strip().lower())
    if form is None:
        raise ChartError(f"unknown chart_type {chart_type!r}; expected one of "
                         f"{', '.join(sorted(FORMS))}")
    if not rows:
        raise ChartError("the query returned no rows, so there is nothing to chart")
    return form(rows, spec or {})


def describe_channels() -> str:
    return "; ".join(f"{name}: {', '.join(chans)}" for name, chans in CHANNELS.items())
