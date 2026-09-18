"""Inline-SVG charts for the printed prescribing report.

Every chart here plots one series — a count — so each follows the same rules:
one colour for every bar (never a value-ramp, which would double-encode length
as hue), no legend (the title names the series), thin marks with a 4px rounded
data-end, hairline solid gridlines, and text in ink tokens rather than the
series colour. Values are direct-labelled at the tip, which is what stands in
for the table view this document cannot make interactive.

The palette is the Phrase Health primary ramp. #9027db (primary-700) was
validated against a white surface with the dataviz validator: it clears the
lightness band, the chroma floor and 3:1 contrast. The four-slot categorical
order, if a chart ever needs more than one series, is
#9027db, #f96116, #2563eb, #16a34a — all six checks pass in light mode.

This is print: no hover layer, no dark mode. Both are deliberate omissions, not
oversights — a PDF has neither a pointer nor a theme.
"""

from __future__ import annotations

import html

SERIES = "#9027db"          # primary-700
INK = "#1f2937"             # --fg-default
INK_MUTED = "#6b7280"       # --fg-muted
GRID = "#e5e7eb"            # --border-default
SURFACE = "#ffffff"
# SERIES as components, for the rgba tints charts.grid() shades with.
SERIES_RGB = (144, 39, 219)

VIEW_W = 620                # SVG user units; scales to the column width
BAR_MAX = 14                # <= 24px thick, capped so the band keeps its air
BAR_GAP = 10                # >= 2px surface gap between adjacent bars
LABEL_W = 324               # left label column; wide enough for a
                            # "DATABASE - Name" label, or a bracketed
                            # "term [drug class]" alias, to survive whole
VALUE_W = 76                # right gutter for the value at the tip
STACK_LABEL_MAX = 190       # cap on stacked()'s row-label gutter. Its labels
                            # name a group rather than a category, so they run
                            # shorter than hbar's — but they must not be
                            # clipped, and the bars need the rest of the width.
LABEL_SIZE = 10             # category labels
# Rough advance width per character at LABEL_SIZE. Deliberately pessimistic:
# these labels are largely upper-case clinical names, wider than the mixed-case
# average, and a clipped label is worse than a truncated one.
CHAR_W = LABEL_SIZE * 0.60
# A 7.5px mono digit; used to decide whether a stacked segment can hold
# its own count without spilling into the neighbouring one.
SEGMENT_CHAR_W = 7.5 * 0.60
# The band a stratum heading occupies, in hbar() and stacked() alike. A full
# bar band rather than something shorter: at 0.9 of the bar height the heading
# had about a pixel between its descenders and the top of the first bar under
# it, which read as overlapping text. The baseline sits low in the band, so the
# space that separates a heading from the group above it is above the text and
# the group it names sits directly beneath it.
HEAD_BAND = BAR_MAX + BAR_GAP
HEAD_BASELINE = HEAD_BAND * 0.66


def esc(text) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def truncate(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def nice_ticks(top: float, count: int = 5):
    """Round axis ticks to clean numbers: 0 / 1,000 / 2,000 …"""
    if top <= 0:
        return [0], 1
    raw = top / count
    magnitude = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 1
    for step in (1, 2, 2.5, 5, 10):
        if magnitude * step >= raw:
            step = magnitude * step
            break
    else:
        step = magnitude * 10
    # The last tick has to be >= top, or the longest bar runs past the plot.
    ticks, value = [0.0], 0.0
    while value < top:
        value += step
        ticks.append(value)
    return ticks, ticks[-1] or 1


def tick_label(value: float, ticks) -> str:
    """Whole numbers where the step is whole; one decimal where it is not.

    A 2.5 step rounded to integers reads 0 2 5 8 10 — the gaps look uneven
    when they are not.

    Large axes are abbreviated. `1,250,000` is nine characters and five of them
    collide with their neighbours at this width; `1.25M` is what a reader wants from
    an axis anyway, since the exact figure is direct-labelled at each bar.
    """
    top = max(ticks) if ticks else 0
    if top >= 1_000_000:
        return _abbrev(value, 1_000_000, "M")
    if top >= 10_000:
        return _abbrev(value, 1_000, "K")
    fractional = any(abs(t - round(t)) > 1e-9 for t in ticks)
    return f"{value:,.1f}" if fractional else f"{value:,.0f}"


def _abbrev(value: float, unit: float, suffix: str) -> str:
    scaled = value / unit
    if value == 0:
        return "0"
    text = f"{scaled:.2f}".rstrip("0").rstrip(".")
    return f"{text}{suffix}"


def _frame(height: int, body: str, title: str = "", note: str = "",
           subtitle: str = "", scale: float = 1.0) -> str:
    """The plot, and only the plot.

    In the source this also emitted the caption, subtitle and note. Here a figure is
    captioned by its position in the report — `Figure 2` — which is not known until
    publish, so the chrome is added there and this returns bare SVG. The title and
    note arguments are kept so the call sites port unchanged; they are ignored.
    """
    width = f"{scale * 100:g}%"
    return (
        f'<svg viewBox="0 0 {VIEW_W} {height}" width="{width}" '
        f'height="{height * scale:.0f}" '
        'role="img" xmlns="http://www.w3.org/2000/svg">'
        f"{body}</svg>"
    )


# --------------------------------------------------------------------------
# horizontal bars — magnitude across named categories
# --------------------------------------------------------------------------
def hbar(pairs, title: str, note: str = "", fmt=None,
         percent_axis: bool = False, subtitle: str = "") -> str:
    """`pairs` is [(label, value), ...] already in the order to draw.

    `fmt(value, label)` renders the value at the tip; the default is a plain
    count. A percentage chart passes its own so the tip can read "62%  (41)" —
    a percentage without its numerator hides the denominator it came from.

    A pair whose value is None is a heading rather than a bar, which is how a
    ranked chart carries strata: the rows stay one list, one axis and one
    ranking rule, and the heading only says where one group ends and the next
    begins. Headings take a shorter band than bars, and are skipped when the
    axis is scaled, so an empty group cannot stretch the axis.
    """
    fmt = fmt or (lambda value, _label: f"{value:,.0f}")
    label_limit = int((LABEL_W - 12) / CHAR_W)
    pairs = [(str(label), None if value is None else float(value))
             for label, value in pairs]
    bars = [v for _l, v in pairs if v is not None]
    if not bars:
        return _frame(40, f'<text x="0" y="24" font-size="12" fill="{INK_MUTED}" '
                          'font-family="DM Sans, sans-serif">No data.</text>',
                      title, note, subtitle)

    band = BAR_MAX + BAR_GAP
    head_band = HEAD_BAND
    top, bottom = 18, 22                      # room for the axis band
    height = int(top + bottom + sum(head_band if v is None else band
                                    for _l, v in pairs))
    plot_w = VIEW_W - LABEL_W - VALUE_W
    ticks, axis_top = nice_ticks(max(bars))
    if percent_axis:
        ticks, axis_top = [0, 25, 50, 75, 100], 100

    out = []
    for tick in ticks:                        # hairline solid grid, recessive
        x = LABEL_W + plot_w * (tick / axis_top)
        out.append(f'<line x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" '
                   f'y2="{height - bottom + 2}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{x:.1f}" y="{height - bottom + 15}" font-size="10" '
                   f'fill="{INK_MUTED}" text-anchor="middle" '
                   'font-family="DM Mono, monospace" '
                   f'style="font-variant-numeric:tabular-nums">'
                   f'{tick_label(tick, ticks)}{"%" if percent_axis else ""}</text>')

    y = top
    for label, value in pairs:
        if value is None:                     # a stratum heading, not a bar
            out.append(
                f'<text x="0" y="{y + HEAD_BASELINE:.1f}" font-size="10" '
                f'fill="{INK}" font-weight="600" '
                'font-family="DM Sans, sans-serif">'
                f"{esc(truncate(label, int(VIEW_W / CHAR_W)))}</text>"
            )
            y += head_band
            continue
        width = plot_w * (value / axis_top) if axis_top else 0
        out.append(
            f'<text x="{LABEL_W - 10}" y="{y + BAR_MAX * 0.78:.1f}" font-size="10" '
            f'fill="{INK}" text-anchor="end" font-family="DM Sans, sans-serif">'
            f"{esc(truncate(label, label_limit))}</text>"
        )
        if width > 0:
            # Square at the baseline, 4px rounded at the data end.
            r = min(4, width)
            out.append(
                f'<path d="M{LABEL_W} {y} H{LABEL_W + width - r:.1f} '
                f'a{r} {r} 0 0 1 {r} {r} V{y + BAR_MAX - r} '
                f'a{r} {r} 0 0 1 -{r} {r} H{LABEL_W} Z" fill="{SERIES}"/>'
            )
        out.append(
            f'<text x="{LABEL_W + width + 8:.1f}" y="{y + BAR_MAX * 0.72:.1f}" '
            f'font-size="10" fill="{INK_MUTED}" '
            'font-family="DM Mono, monospace" '
            f'style="font-variant-numeric:tabular-nums">{esc(fmt(value, label))}</text>'
        )
        y += band
    out.append(f'<line x1="{LABEL_W}" y1="{top - 6}" x2="{LABEL_W}" '
               f'y2="{height - bottom + 2}" stroke="{GRID}" stroke-width="1"/>')
    return _frame(height, "".join(out), title, note, subtitle)


# --------------------------------------------------------------------------
# sankey — where one grouping's population ends up in another
# --------------------------------------------------------------------------
def sankey(flows, left_order, right_order, title: str, note: str = "",
           subtitle: str = "", colours=None, unit: str = "patients") -> str:
    """`flows` is {(left, right): value}; the two orders fix the node stacking.

    A stacked bar already answers "what share of this group went where", and
    for one grouping it answers it better — shorter, easier to read a
    percentage off. What it cannot show is the other direction: how a
    destination's intake divides across the sources. A Sankey carries both,
    because a ribbon has two ends and the node it lands in is the sum of what
    arrives.

    Ribbons take the DESTINATION's colour, not the source's. The question here
    is where a population went, so the destination is the entity being tracked
    and the right-hand nodes match the stacked chart this restates — the two
    figures can be read against each other without re-learning a palette.

    Both sides carry the same total by construction (every patient is in
    exactly one flow), so one scale serves both columns and a node's height is
    comparable across the diagram.
    """
    flows = {k: float(v) for k, v in flows.items() if v}
    if not flows:
        return _frame(40, f'<text x="0" y="24" font-size="12" fill="{INK_MUTED}" '
                          'font-family="DM Sans, sans-serif">No data.</text>',
                      title, note, subtitle)

    left_order = [n for n in left_order
                  if any(l == n for l, _r in flows)]
    right_order = [n for n in right_order
                   if any(r == n for _l, r in flows)]
    colours = colours or categorical(right_order)

    left_total = {n: sum(v for (l, _r), v in flows.items() if l == n)
                  for n in left_order}
    right_total = {n: sum(v for (_l, r), v in flows.items() if r == n)
                   for n in right_order}
    grand = sum(flows.values())

    # Geometry. The label gutters are sized to the longest name on each side,
    # capped, because a Sankey with clipped node labels is unreadable in a way
    # a bar chart is not — there is no axis to fall back on.
    node_w, gap = 13, 9
    left_w = min(190, max(len(n) for n in left_order) * CHAR_W + 56)
    right_w = min(190, max(len(n) for n in right_order) * CHAR_W + 56)
    top, bottom = 14, 16
    plot_h = max(190, 26 * max(len(left_order), len(right_order)))
    height = top + plot_h + bottom
    left_x = left_w
    right_x = VIEW_W - right_w - node_w

    def scale(side):
        """Pixels per unit, once the inter-node gaps are taken out."""
        usable = plot_h - gap * (len(side) - 1)
        return usable / grand if grand else 0.0

    per_unit = min(scale(left_order), scale(right_order))

    out = []
    # Node tops, and a running cursor inside each node for the ribbon ends.
    def stack(order, totals):
        tops, y = {}, top
        for name in order:
            tops[name] = y
            y += totals[name] * per_unit + gap
        return tops

    left_top = stack(left_order, left_total)
    right_top = stack(right_order, right_total)
    left_cursor = dict(left_top)
    right_cursor = dict(right_top)

    # Ribbons first, so the nodes draw over their ends. Ordered by the right
    # stacking within each left node, which is what makes the two columns
    # read as one flow rather than a tangle.
    for left in left_order:
        for right in right_order:
            value = flows.get((left, right))
            if not value:
                continue
            thickness = value * per_unit
            y0, y1 = left_cursor[left], right_cursor[right]
            left_cursor[left] += thickness
            right_cursor[right] += thickness
            x0, x1 = left_x + node_w, right_x
            mid = (x0 + x1) / 2
            out.append(
                f'<path d="M{x0:.1f} {y0:.1f} '
                f'C{mid:.1f} {y0:.1f} {mid:.1f} {y1:.1f} {x1:.1f} {y1:.1f} '
                f'L{x1:.1f} {y1 + thickness:.1f} '
                f'C{mid:.1f} {y1 + thickness:.1f} {mid:.1f} {y0 + thickness:.1f} '
                f'{x0:.1f} {y0 + thickness:.1f} Z" '
                f'fill="{colours.get(right, OTHER)}" fill-opacity="0.42"/>'
            )

    # Nodes and their labels.
    for name in left_order:
        h = left_total[name] * per_unit
        y = left_top[name]
        out.append(f'<rect x="{left_x}" y="{y:.1f}" width="{node_w}" '
                   f'height="{max(h, 1):.1f}" fill="{INK_MUTED}"/>')
        out.append(
            f'<text x="{left_x - 8}" y="{y + h / 2 + 3:.1f}" font-size="10" '
            f'fill="{INK}" text-anchor="end" '
            f'font-family="DM Sans, sans-serif">'
            f'{esc(truncate(name, int((left_w - 56) / CHAR_W)))}'
            f'<tspan fill="{INK_MUTED}" font-family="DM Mono, monospace" '
            'style="font-variant-numeric:tabular-nums">  '
            f'{left_total[name]:,.0f}</tspan></text>'
        )
    for name in right_order:
        h = right_total[name] * per_unit
        y = right_top[name]
        out.append(f'<rect x="{right_x}" y="{y:.1f}" width="{node_w}" '
                   f'height="{max(h, 1):.1f}" fill="{colours.get(name, OTHER)}"/>')
        out.append(
            f'<text x="{right_x + node_w + 8}" y="{y + h / 2 + 3:.1f}" '
            f'font-size="10" fill="{INK}" '
            f'font-family="DM Sans, sans-serif">'
            f'{esc(truncate(name, int((right_w - 56) / CHAR_W)))}'
            f'<tspan fill="{INK_MUTED}" font-family="DM Mono, monospace" '
            'style="font-variant-numeric:tabular-nums">  '
            f'{right_total[name]:,.0f} ({100 * right_total[name] / grand:.0f}%)'
            '</tspan></text>'
        )
    return _frame(int(height), "".join(out), title, note, subtitle)


# --------------------------------------------------------------------------
# signed bars — a difference that can fall either way
# --------------------------------------------------------------------------
# A risk difference is not a magnitude, and hbar() cannot draw one: it grows
# every bar rightwards from a zero at the left edge, so a negative value would
# render as nothing at all. A difference has a direction, and the direction is
# half the finding — "this group's long-stay rate is eight points LOWER" is a
# different sentence from "eight points higher", and a chart that can only draw
# one of them either hides the other or, worse, draws it the same way.
#
# So: a diverging form. Zero is a line inside the plot, bars grow away from it
# both ways, and the two directions take the two poles of a diverging pair —
# warm for the direction the chart is about (more), cool for its opposite
# (less). Both are steps already validated against the white surface, and the
# pair clears CVD separation at ΔE 24.7 (protan) / 32.7 (tritan) with a
# normal-vision ΔE of 33.6. The neutral gray is for a value too small to mean
# anything, so a rounding artefact does not get a colour that reads as a
# direction.
DIVERGE_MORE = "#eb6834"     # warm pole: the outcome is more common
DIVERGE_LESS = "#2a78d6"     # cool pole: the outcome is less common
DIVERGE_NONE = "#9ca3af"     # neutral: inside the negligible band


def signed_hbar(pairs, title: str, note: str = "", fmt=None,
                subtitle: str = "", unit: str = "", negligible: float = 0.0,
                axis_label: str = "") -> str:
    """`pairs` is [(label, value), ...] in the order to draw; value may be < 0.

    Same conventions as hbar() — a None value is a stratum heading, `fmt`
    renders the text at the tip — with the zero line moved inside the plot and
    scaled symmetrically, so a +8 and a -8 are the same length. Symmetry is
    the point: an axis scaled to the data's own range on each side would make
    a small negative look as decisive as a large positive.

    `negligible` greys out bars whose absolute value is under it, for a
    difference that is real arithmetic but not a finding.
    """
    fmt = fmt or (lambda value, _label: f"{value:+,.1f}{unit}")
    label_limit = int((LABEL_W - 12) / CHAR_W)
    pairs = [(str(label), None if value is None else float(value))
             for label, value in pairs]
    bars = [v for _l, v in pairs if v is not None]
    if not bars:
        return _frame(40, f'<text x="0" y="24" font-size="12" fill="{INK_MUTED}" '
                          'font-family="DM Sans, sans-serif">No data.</text>',
                      title, note, subtitle)

    band, head_band = BAR_MAX + BAR_GAP, HEAD_BAND
    top, bottom = 18, 34 if axis_label else 22
    height = int(top + bottom + sum(head_band if v is None else band
                                    for _l, v in pairs))
    # The value text sits at the tip, which for a negative bar is on the LEFT
    # of the zero line — so both gutters have to hold it, not just the right.
    plot_w = VIEW_W - LABEL_W - VALUE_W
    extent = max(abs(v) for v in bars) or 1.0
    ticks, half = nice_ticks(extent, 3)
    zero_x = LABEL_W + plot_w / 2
    half_w = plot_w / 2

    def x_of(value):
        return zero_x + half_w * max(-1.0, min(1.0, value / half))

    out = []
    for tick in ticks:
        for signed in ({-tick, tick} if tick else {0.0}):
            x = x_of(signed)
            out.append(f'<line x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" '
                       f'y2="{height - bottom + 2}" stroke="{GRID}" '
                       'stroke-width="1"/>')
            out.append(f'<text x="{x:.1f}" y="{height - bottom + 15}" '
                       f'font-size="10" fill="{INK_MUTED}" '
                       'text-anchor="middle" font-family="DM Mono, monospace" '
                       'style="font-variant-numeric:tabular-nums">'
                       f'{signed:+g}{esc(unit)}</text>'
                       if signed else
                       f'<text x="{x:.1f}" y="{height - bottom + 15}" '
                       f'font-size="10" fill="{INK_MUTED}" '
                       'text-anchor="middle" font-family="DM Mono, monospace">'
                       f'0{esc(unit)}</text>')
    if axis_label:
        out.append(f'<text x="{zero_x:.1f}" y="{height - bottom + 29}" '
                   f'font-size="9" fill="{INK_MUTED}" text-anchor="middle" '
                   f'font-family="DM Sans, sans-serif">{esc(axis_label)}</text>')

    y = top
    for label, value in pairs:
        if value is None:
            out.append(
                f'<text x="0" y="{y + HEAD_BASELINE:.1f}" font-size="10" '
                f'fill="{INK}" font-weight="600" '
                'font-family="DM Sans, sans-serif">'
                f"{esc(truncate(label, int(VIEW_W / CHAR_W)))}</text>"
            )
            y += head_band
            continue
        out.append(
            f'<text x="{LABEL_W - 10}" y="{y + BAR_MAX * 0.78:.1f}" '
            f'font-size="10" fill="{INK}" text-anchor="end" '
            f'font-family="DM Sans, sans-serif">'
            f"{esc(truncate(label, label_limit))}</text>"
        )
        tip = x_of(value)
        colour = (DIVERGE_NONE if abs(value) < negligible
                  else DIVERGE_MORE if value > 0 else DIVERGE_LESS)
        width = abs(tip - zero_x)
        if width > 0:
            r = min(4, width)
            if value > 0:                     # square at zero, rounded at the tip
                out.append(
                    f'<path d="M{zero_x:.1f} {y} H{tip - r:.1f} '
                    f'a{r} {r} 0 0 1 {r} {r} V{y + BAR_MAX - r} '
                    f'a{r} {r} 0 0 1 -{r} {r} H{zero_x:.1f} Z" '
                    f'fill="{colour}"/>'
                )
            else:
                out.append(
                    f'<path d="M{zero_x:.1f} {y} H{tip + r:.1f} '
                    f'a{r} {r} 0 0 0 -{r} {r} V{y + BAR_MAX - r} '
                    f'a{r} {r} 0 0 0 {r} {r} H{zero_x:.1f} Z" '
                    f'fill="{colour}"/>'
                )
        # Direct-labelled outwards from zero, so the text never sits on a bar.
        anchor_end = value < 0
        text_x = tip - 8 if anchor_end else tip + 8
        out.append(
            f'<text x="{text_x:.1f}" y="{y + BAR_MAX * 0.72:.1f}" '
            f'font-size="10" fill="{INK_MUTED}" '
            f'text-anchor="{"end" if anchor_end else "start"}" '
            'font-family="DM Mono, monospace" '
            'style="font-variant-numeric:tabular-nums">'
            f'{esc(fmt(value, label))}</text>'
        )
        y += band
    out.append(f'<line x1="{zero_x:.1f}" y1="{top - 6}" x2="{zero_x:.1f}" '
               f'y2="{height - bottom + 2}" stroke="{INK_MUTED}" '
               'stroke-width="1"/>')
    return _frame(height, "".join(out), title, note, subtitle)


# --------------------------------------------------------------------------
# columns — a distribution over ordered bands
# --------------------------------------------------------------------------
def vbar(pairs, title: str, note: str = "", axis_label: str = "",
         subtitle: str = "") -> str:
    pairs = [(str(label), float(value)) for label, value in pairs]
    if not pairs:
        return _frame(40, f'<text x="0" y="24" font-size="12" fill="{INK_MUTED}" '
                          'font-family="DM Sans, sans-serif">No data.</text>',
                      title, note, subtitle)

    left, right, top, bottom = 46, 8, 16, 40
    plot_h, height = 150, 206
    plot_w = VIEW_W - left - right
    band = plot_w / len(pairs)
    width = min(BAR_MAX + 6, band - BAR_GAP)
    ticks, axis_top = nice_ticks(max(v for _l, v in pairs))

    out = []
    for tick in ticks:
        y = top + plot_h * (1 - tick / axis_top)
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{VIEW_W - right}" '
                   f'y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{left - 8}" y="{y + 3.5:.1f}" font-size="10" '
                   f'fill="{INK_MUTED}" text-anchor="end" '
                   'font-family="DM Mono, monospace" '
                   f'style="font-variant-numeric:tabular-nums">{tick_label(tick, ticks)}</text>')

    for i, (label, value) in enumerate(pairs):
        x = left + band * i + (band - width) / 2
        h = plot_h * (value / axis_top) if axis_top else 0
        y = top + plot_h - h
        if h > 0:
            r = min(4, h)
            out.append(
                f'<path d="M{x:.1f} {top + plot_h} V{y + r:.1f} '
                f'a{r} {r} 0 0 1 {r} -{r} H{x + width - r:.1f} '
                f'a{r} {r} 0 0 1 {r} {r} V{top + plot_h} Z" fill="{SERIES}"/>'
            )
        out.append(
            f'<text x="{x + width / 2:.1f}" y="{y - 5:.1f}" font-size="9" '
            f'fill="{INK_MUTED}" text-anchor="middle" '
            'font-family="DM Mono, monospace" '
            f'style="font-variant-numeric:tabular-nums">{value:,.0f}</text>'
        )
        out.append(
            f'<text x="{x + width / 2:.1f}" y="{top + plot_h + 15}" font-size="10" '
            f'fill="{INK}" text-anchor="middle" '
            f'font-family="DM Sans, sans-serif">{esc(label)}</text>'
        )
    if axis_label:
        out.append(f'<text x="{left + plot_w / 2:.1f}" y="{height - 6}" '
                   f'font-size="10" fill="{INK_MUTED}" text-anchor="middle" '
                   f'font-family="DM Sans, sans-serif">{esc(axis_label)}</text>')
    return _frame(height, "".join(out), title, note, subtitle)


# --------------------------------------------------------------------------
# lines — two or more series over the same x axis
# --------------------------------------------------------------------------
def lines(x_labels, series, title: str, note: str = "", subtitle: str = "",
          tick_every: int = 0, axis_label: str = "") -> str:
    """`series` is [(name, [value per x label]), ...] on the shared axis.

    line()'s multi-series sibling, and separate rather than a flag on it for
    two reasons that both change the drawing rather than just the count.
    First, line() fills the area under its series at 10% — with two series
    that fill overlaps and the reader cannot tell a crossing from an
    occlusion, so nothing is filled here. Second, one series needs no legend
    because the title names it, whereas two or more must never rest on colour
    alone: this form carries both a legend and a direct label at the end of
    each line, so the series can still be told apart in grayscale, and in
    print there is no hover layer to fall back on.

    A None in a series is a gap, not a zero — a product that did not exist yet
    has no value, and joining across it would draw a rise out of nothing.
    """
    x_labels = [str(label) for label in x_labels]
    series = [(str(name), list(values)) for name, values in series
              if any(v is not None for v in values)]
    if len(x_labels) < 2 or not series:
        return _frame(40, f'<text x="0" y="24" font-size="12" fill="{INK_MUTED}" '
                          'font-family="DM Sans, sans-serif">No data.</text>',
                      title, note, subtitle)

    names = [name for name, _v in series]
    colours = categorical(names)
    # The right gutter holds the end-of-line label, so it is wide enough for
    # the longest series name rather than the 12 units line() can spare.
    label_w = min(150, max(len(name) for name in names) * CHAR_W + 14)
    left, right, top, bottom = 46, 12 + label_w, 18, 34
    plot_h, height = 140, 192
    plot_w = VIEW_W - left - right
    highest = max((v for _n, values in series for v in values
                   if v is not None), default=0)
    ticks, axis_top = nice_ticks(highest)
    step = plot_w / (len(x_labels) - 1)

    out = []
    for tick in ticks:
        y = top + plot_h * (1 - tick / axis_top)
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w:.1f}" '
                   f'y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{left - 8}" y="{y + 3.5:.1f}" font-size="10" '
                   f'fill="{INK_MUTED}" text-anchor="end" '
                   'font-family="DM Mono, monospace" '
                   f'style="font-variant-numeric:tabular-nums">'
                   f'{tick_label(tick, ticks)}</text>')

    def y_of(value):
        return top + plot_h * (1 - value / axis_top) if axis_top else top + plot_h

    ends = []
    for name, values in series:
        colour = colours[name]
        # One <path> per unbroken run, so a gap is a gap rather than a
        # straight line drawn through months the product did not exist in.
        run, last_index = [], None
        for i, value in enumerate(values[: len(x_labels)]):
            if value is None:
                if len(run) > 1:
                    out.append('<path d="M'
                               + " L".join(f"{x:.1f} {y:.1f}" for x, y in run)
                               + f'" fill="none" stroke="{colour}" '
                               'stroke-width="2" stroke-linejoin="round" '
                               'stroke-linecap="round"/>')
                elif len(run) == 1:
                    # A single point between two gaps would be invisible as a
                    # path, so it is drawn as a mark instead.
                    out.append(f'<circle cx="{run[0][0]:.1f}" '
                               f'cy="{run[0][1]:.1f}" r="2.5" fill="{colour}"/>')
                run = []
                continue
            run.append((left + step * i, y_of(float(value))))
            last_index = i
        if len(run) > 1:
            out.append('<path d="M'
                       + " L".join(f"{x:.1f} {y:.1f}" for x, y in run)
                       + f'" fill="none" stroke="{colour}" stroke-width="2" '
                       'stroke-linejoin="round" stroke-linecap="round"/>')
        elif len(run) == 1:
            out.append(f'<circle cx="{run[0][0]:.1f}" cy="{run[0][1]:.1f}" '
                       f'r="2.5" fill="{colour}"/>')
        if last_index is not None:
            ends.append((name, colour, left + step * last_index,
                         y_of(float(values[last_index])),
                         float(values[last_index])))

    # Direct labels at the end of each line. Nudged apart when two series
    # finish at the same height, which on a monthly count is common — and two
    # labels drawn over each other is worse than either being slightly off
    # its own line.
    ends.sort(key=lambda row: row[3])
    placed = []
    for name, colour, x, y, value in ends:
        text_y = y + 3.5
        for other in placed:
            if abs(text_y - other) < 12:
                text_y = other + 12
        placed.append(text_y)
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{colour}" '
                   f'stroke="{SURFACE}" stroke-width="2"/>')
        out.append(f'<text x="{x + 9:.1f}" y="{text_y:.1f}" font-size="10" '
                   f'fill="{INK}" font-family="DM Sans, sans-serif">'
                   f'{esc(truncate(name, int(label_w / CHAR_W)))}'
                   '<tspan font-family="DM Mono, monospace" '
                   f'fill="{INK_MUTED}" '
                   'style="font-variant-numeric:tabular-nums"> '
                   f'{value:,.0f}</tspan></text>')

    every = tick_every or max(1, len(x_labels) // 8)
    last = len(x_labels) - 1
    for i, label in enumerate(x_labels):
        if i % every and i != last:
            continue
        anchor = "start" if i == 0 else "end" if i == last else "middle"
        out.append(f'<text x="{left + step * i:.1f}" y="{top + plot_h + 16}" '
                   f'font-size="9" fill="{INK_MUTED}" text-anchor="{anchor}" '
                   f'font-family="DM Sans, sans-serif">{esc(label)}</text>')
        out.append(f'<line x1="{left + step * i:.1f}" y1="{top + plot_h}" '
                   f'x2="{left + step * i:.1f}" y2="{top + plot_h + 4}" '
                   f'stroke="{GRID}" stroke-width="1"/>')
    if axis_label:
        out.append(f'<text x="{left + plot_w / 2:.1f}" y="{height - 4}" '
                   f'font-size="9" fill="{INK_MUTED}" text-anchor="middle" '
                   f'font-family="DM Sans, sans-serif">{esc(axis_label)}</text>')

    # Inline rather than through _frame, which has no legend slot — the same
    # shape stacked() and vstacked() return, so all three read identically.
    return (
        '<figure class="chart">'
        f"{caption(title)}"
        + (f'<p class="chart__subtitle">{esc(subtitle)}</p>' if subtitle else "")
        + legend(names, colours)
        + f'<svg viewBox="0 0 {VIEW_W} {height}" width="100%" '
          f'height="{height}" role="img" xmlns="http://www.w3.org/2000/svg">'
        + "".join(out) + "</svg>"
        + (f'<p class="chart__note">{esc(note)}</p>' if note else "")
        + "</figure>"
    )


# --------------------------------------------------------------------------
# vstacked — a stacked histogram: one column per bin, split into categories
# --------------------------------------------------------------------------
def vstacked(bins, categories, title: str, note: str = "",
             axis_label: str = "", subtitle: str = "",
             tick_every: int = 1) -> str:
    """One vertical column per bin, stacked by category, counts on the y axis.

    `bins` is [(label, {category: count}), ...] in x order. stacked() puts the
    bars horizontally, which is right for comparing a handful of named groups;
    a histogram needs its scale on the x axis instead, so this is the vertical
    sibling rather than a flag on that one.

    `tick_every` thins the x labels when there are more bins than labels that
    will fit, so a 30-bin histogram still reads.
    """
    bins = [(str(label), dict(values)) for label, values in bins]
    if not bins:
        return _frame(40, f'<text x="0" y="24" font-size="12" fill="{INK_MUTED}" '
                          'font-family="DM Sans, sans-serif">No data.</text>',
                      title, note, subtitle)

    colours = ramp(categories)
    left, right, top, bottom = 46, 8, 16, 52
    plot_h, height = 150, 218
    plot_w = VIEW_W - left - right
    band = plot_w / len(bins)
    width = max(2.0, min(BAR_MAX + 10, band - 3))
    totals = [sum(v.get(c, 0) for c in categories) for _l, v in bins]
    ticks, axis_top = nice_ticks(max(totals) if totals else 1)

    out = []
    for tick in ticks:
        y = top + plot_h * (1 - tick / axis_top)
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{VIEW_W - right}" '
                   f'y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{left - 8}" y="{y + 3.5:.1f}" font-size="10" '
                   f'fill="{INK_MUTED}" text-anchor="end" '
                   'font-family="DM Mono, monospace" '
                   'style="font-variant-numeric:tabular-nums">'
                   f'{tick_label(tick, ticks)}</text>')

    for i, (label, values) in enumerate(bins):
        x = left + band * i + (band - width) / 2
        cursor = top + plot_h
        for category in categories:
            value = values.get(category, 0)
            if not value:
                continue
            h = plot_h * (value / axis_top) if axis_top else 0
            cursor -= h
            out.append(
                f'<rect x="{x:.1f}" y="{cursor:.1f}" width="{width:.1f}" '
                f'height="{h:.2f}" fill="{colours[category]}"/>'
            )
        total = totals[i]
        if total:
            out.append(
                f'<text x="{x + width / 2:.1f}" y="{cursor - 4:.1f}" '
                f'font-size="8" fill="{INK_MUTED}" text-anchor="middle" '
                'font-family="DM Mono, monospace" '
                f'style="font-variant-numeric:tabular-nums">{total:,.0f}</text>'
            )
        if i % tick_every == 0:
            out.append(
                f'<text x="{x + width / 2:.1f}" y="{top + plot_h + 14}" '
                f'font-size="9" fill="{INK}" text-anchor="middle" '
                f'font-family="DM Sans, sans-serif">{esc(label)}</text>'
            )
    if axis_label:
        out.append(f'<text x="{left + plot_w / 2:.1f}" y="{top + plot_h + 30}" '
                   f'font-size="10" fill="{INK_MUTED}" text-anchor="middle" '
                   f'font-family="DM Sans, sans-serif">{esc(axis_label)}</text>')
    # Built inline rather than through _frame, which has no legend slot —
    # the same shape stacked() returns, so the two read identically on a page.
    return (
        '<figure class="chart">'
        f"{caption(title)}"
        + (f'<p class="chart__subtitle">{esc(subtitle)}</p>' if subtitle else "")
        + legend(categories, colours)
        + f'<svg viewBox="0 0 {VIEW_W} {height}" width="100%" height="{height}" '
          'role="img" xmlns="http://www.w3.org/2000/svg">'
        + "".join(out) + "</svg>"
        # esc(), as _frame() and the others do. A chart note is plain text:
        # it usually carries a medication or component name straight from the
        # data, and one contract for the parameter beats two.
        + (f'<p class="chart__note">{esc(note)}</p>' if note else "")
        + "</figure>"
    )


# --------------------------------------------------------------------------
# line — one series over time
# --------------------------------------------------------------------------
def line(pairs, title: str, note: str = "", subtitle: str = "") -> str:
    pairs = [(str(label), float(value)) for label, value in pairs]
    if len(pairs) < 2:
        return vbar(pairs, title, note)

    left, right, top, bottom = 46, 12, 18, 34
    plot_h, height = 140, 192
    plot_w = VIEW_W - left - right
    ticks, axis_top = nice_ticks(max(v for _l, v in pairs))
    step = plot_w / (len(pairs) - 1)

    out = []
    for tick in ticks:
        y = top + plot_h * (1 - tick / axis_top)
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{VIEW_W - right}" '
                   f'y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{left - 8}" y="{y + 3.5:.1f}" font-size="10" '
                   f'fill="{INK_MUTED}" text-anchor="end" '
                   'font-family="DM Mono, monospace" '
                   f'style="font-variant-numeric:tabular-nums">{tick_label(tick, ticks)}</text>')

    points = [
        (left + step * i, top + plot_h * (1 - value / axis_top))
        for i, (_l, value) in enumerate(pairs)
    ]
    area = (f'M{points[0][0]:.1f} {top + plot_h} '
            + " ".join(f"L{x:.1f} {y:.1f}" for x, y in points)
            + f' L{points[-1][0]:.1f} {top + plot_h} Z')
    out.append(f'<path d="{area}" fill="{SERIES}" fill-opacity="0.10"/>')
    out.append('<path d="' + "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in points)
               + f'" fill="none" stroke="{SERIES}" stroke-width="2" '
               'stroke-linejoin="round" stroke-linecap="round"/>')

    # Label the endpoint and the peak only — never every point.
    last = len(pairs) - 1
    peak = max(range(len(pairs)), key=lambda i: pairs[i][1])
    marked = {last} | ({peak} if abs(peak - last) > 1 else set())
    for i in marked:
        x, y = points[i]
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{SERIES}" '
                   f'stroke="{SURFACE}" stroke-width="2"/>')
        anchor = "end" if i == last else "middle"
        out.append(f'<text x="{x:.1f}" y="{y - 10:.1f}" font-size="10" '
                   f'fill="{INK_MUTED}" text-anchor="{anchor}" '
                   'font-family="DM Mono, monospace" '
                   f'style="font-variant-numeric:tabular-nums">'
                   f'{pairs[i][1]:,.0f}</text>')

    every = max(1, len(pairs) // 8)
    for i, (label, _v) in enumerate(pairs):
        if i % every and i != last:
            continue
        # The end ticks anchor inwards so they cannot overflow the plot.
        anchor = "start" if i == 0 else "end" if i == last else "middle"
        out.append(f'<text x="{points[i][0]:.1f}" y="{top + plot_h + 16}" '
                   f'font-size="9" fill="{INK_MUTED}" text-anchor="{anchor}" '
                   f'font-family="DM Sans, sans-serif">{esc(label)}</text>')
    return _frame(height, "".join(out), title, note, subtitle)


# --------------------------------------------------------------------------
# stat tiles — when the answer is a number, not a chart
# --------------------------------------------------------------------------
def stat_tiles(tiles) -> str:
    cells = "".join(
        '<div class="stat">'
        f'<div class="stat__value">{esc(value)}</div>'
        f'<div class="stat__label">{esc(label)}</div>'
        + (f'<div class="stat__hint">{esc(hint)}</div>' if hint else "")
        + "</div>"
        for label, value, hint in tiles
    )
    return f'<div class="stats">{cells}</div>'


# --------------------------------------------------------------------------
# aggregation helpers
# --------------------------------------------------------------------------
def top_n(counter, limit, other_label="Other"):
    """Ordered [(label, count)] with the tail folded into one row.

    Returns (pairs, hidden_categories, hidden_total) so the chart can say what
    it folded rather than silently truncating.
    """
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0]).lower()))
    if not limit or len(ranked) <= limit:
        return ranked, 0, 0
    head, tail = ranked[:limit], ranked[limit:]
    hidden_total = sum(v for _k, v in tail)
    return head + [(other_label, hidden_total)], len(tail), hidden_total


# --------------------------------------------------------------------------
# composition over an ordered sequence
# --------------------------------------------------------------------------
# Therapy classes are ordinal — they read as an era, oldest modality first —
# so they take a one-hue ramp rather than categorical slots. These five steps
# of the Phrase primary scale pass the ordinal checks against a white surface:
# monotone lightness, every adjacent gap >= 0.06, light end clear of 2:1, one
# hue. Five is the ceiling: a sixth step from this ramp fails the gap check, so
# a sixth class folds into OTHER, which is grey and outside the ramp on purpose.
ORDINAL = ("#bc9bff", "#a753ff", "#9027db", "#7223b0", "#29103c")
OTHER = "#888888"                                            # --dark-400

# A twelve-slot CATEGORICAL palette, for a chart whose series have no order —
# provider specialties, say, where nothing makes Cardiology "after" Pulmonary.
# Distinct from ORDINAL above, which is one hue light-to-dark and is correct
# only where the categories *are* a sequence AND there are no more of them
# than the ramp has steps. The therapy classes were on the ramp and are not:
# seven of them against five steps put two on the residual grey, and the
# ordering the ramp claimed placed a bispecific antibody inside a
# FVIII-generation sequence. They take this palette now.
#
# Validated with the dataviz skill's checker against this file's white surface,
# in this order, on the adjacent pairlist that a stacked bar and a legend
# actually exercise:
#
#   lightness band      all 12 inside L 0.43-0.77      PASS
#   chroma floor        all 12 >= 0.1                  PASS
#   CVD separation      worst adjacent dE 9.1 (protan) PASS
#   normal-vision       worst adjacent dE 18.4         PASS
#   contrast vs surface three slots under 3:1          WARN -> relief required
#
# The order is load-bearing: the checks run on adjacent pairs, and moving a
# slot can break them (olive beside red fails CVD separation at dE 3.1). Add a
# hue only by re-running the validator on the new order.
#
# The contrast WARN is discharged rather than ignored: every chart using this
# carries a legend, the segment counts are printed inside the segments, and the
# report puts the same numbers in a table. Identity never rests on hue alone.
CATEGORICAL = (
    "#2a78d6",   # blue
    "#eb6834",   # orange
    "#1baf7a",   # aqua
    "#eda100",   # yellow
    "#e87ba4",   # magenta
    "#008300",   # green
    "#4a3aa7",   # violet
    "#e34948",   # red
    "#00a0c6",   # cyan
    "#a35a1f",   # brown
    "#7a9a01",   # olive
    "#b8479e",   # orchid
)
SEGMENT_GAP = 2                                              # surface gap


NEUTRAL_NAMES = ("not recorded", "other", "unclassified", "unknown",
                 "neither")


def grid(cells, row_labels, col_labels, title: str, note: str = "",
         subtitle: str = "", row_heading: str = "", col_heading: str = "",
         diagonal_blank: bool = False) -> str:
    """A count per (row, column) pair, shaded by magnitude.

    For a transition matrix — who moved from what to what — this is the form
    that fits the question. The same data as a ranked bar chart of "A -> B"
    strings, but a bar chart of N^2 pairs spends its length axis on a value the
    reader must first parse out of a label, folds the tail into "Other", and
    loses the structure entirely: which classes are left, which are arrived at,
    and which pairs never happen at all. A grid keeps all four.

    `cells` is {(row, column): count}. Shading is a single-hue ramp on the
    series colour, so it double-encodes nothing — the number is in the cell and
    the tint is only a way to find the large ones at a glance. An empty pair is
    left blank rather than shown as 0: "never happened" and "happened zero
    times" are the same fact, and blank reads faster.

    `diagonal_blank` greys row == column, for a matrix where staying put is not
    a transition and the diagonal is structurally empty.
    """
    values = [v for v in cells.values() if v]
    peak = max(values) if values else 0
    row_totals = {r: sum(cells.get((r, c), 0) for c in col_labels)
                  for r in row_labels}
    col_totals = {c: sum(cells.get((r, c), 0) for r in row_labels)
                  for c in col_labels}

    head = "".join(
        f'<th class="grid__col">{esc(truncate(str(c), 22))}</th>'
        for c in col_labels
    )
    body = ""
    for r in row_labels:
        cells_html = ""
        for c in col_labels:
            value = cells.get((r, c), 0)
            if diagonal_blank and r == c:
                cells_html += '<td class="grid__cell grid__cell--self">·</td>'
            elif not value:
                cells_html += '<td class="grid__cell grid__cell--none"></td>'
            else:
                # Tint from 10% to 100% of the series colour, as an rgba
                # background rather than opacity on the cell: opacity would
                # fade the number with the fill, and a count of 1 would end up
                # unreadable in the palest cell. The floor keeps a 1 visibly
                # shaded rather than reading as empty.
                alpha = 0.10 + 0.90 * (value / peak if peak else 0)
                # Ink on the pale tints, surface on the dark ones — a single
                # text colour cannot clear 4.5:1 against both ends of a ramp.
                fill = SURFACE if alpha > 0.55 else INK
                cells_html += (
                    f'<td class="grid__cell" style="background:rgba('
                    f'{SERIES_RGB[0]},{SERIES_RGB[1]},{SERIES_RGB[2]},'
                    f'{alpha:.2f});color:{fill}">'
                    f'<span class="grid__n">{value:,}</span></td>'
                )
        body += (
            f'<tr><th class="grid__row">{esc(str(r))}</th>{cells_html}'
            f'<td class="grid__total">{row_totals[r]:,}</td></tr>'
        )
    foot = "".join(f'<td class="grid__total">{col_totals[c]:,}</td>'
                   for c in col_labels)

    return (
        '<figure class="chart">'
        f"{caption(title)}"
        + (f'<p class="chart__subtitle">{esc(subtitle)}</p>' if subtitle else "")
        + '<table class="grid">'
        + '<thead><tr><th class="grid__corner">'
        + (f'<span class="grid__axis">{esc(col_heading)} &rarr;</span>'
           if col_heading else "")
        + (f'<span class="grid__axis grid__axis--row">&darr; {esc(row_heading)}'
           "</span>" if row_heading else "")
        + f'</th>{head}<th class="grid__col grid__col--total">Total</th></tr>'
        + "</thead>"
        + f"<tbody>{body}</tbody>"
        + f'<tfoot><tr><th class="grid__row">Total</th>{foot}'
        + f'<td class="grid__total">{sum(values):,}</td></tr></tfoot>'
        + "</table>"
        + (f'<p class="chart__note">{esc(note)}</p>' if note else "")
        + "</figure>"
    )


def ramp(categories):
    """{category: colour} in the given order.

    A residual category — "Not recorded", "Other" — takes the grey and does
    not consume a ramp step: it is not part of the sequence the ramp encodes.
    The meaningful categories then spread across the whole ramp, so two of them
    take its ends rather than two neighbouring steps that barely differ.
    """
    meaningful = [c for c in categories if str(c).lower() not in NEUTRAL_NAMES]
    # Past the ramp's length the tail takes the grey. Spreading further would
    # hand two classes the same step, which reads as one class.
    ramped, tail = meaningful[: len(ORDINAL)], meaningful[len(ORDINAL):]
    if len(ramped) == 1:
        picks = [len(ORDINAL) - 1]
    elif ramped:
        step = (len(ORDINAL) - 1) / (len(ramped) - 1)
        picks = [round(i * step) for i in range(len(ramped))]
    else:
        picks = []
    colours = {name: ORDINAL[p] for name, p in zip(ramped, picks)}
    for name in list(tail) + list(categories):
        colours.setdefault(name, OTHER)
    return colours


def categorical(categories):
    """{category: colour} from CATEGORICAL, in the given order.

    The counterpart to ramp() for series with no order. A residual category
    takes the grey and does not consume a slot, exactly as in ramp(), and a
    thirteenth meaningful category takes the grey too — the palette is not
    cycled, because a repeated hue reads as a repeated series. Fold the tail
    into one residual rather than letting several share a colour.
    """
    meaningful = [c for c in categories
                  if str(c).lower() not in NEUTRAL_NAMES]
    colours = {name: CATEGORICAL[i]
               for i, name in enumerate(meaningful[:len(CATEGORICAL)])}
    for name in categories:
        colours.setdefault(name, OTHER)
    return colours


def legend(categories, colours) -> str:
    """Identity never rests on colour alone, so two or more series get this."""
    keys = "".join(
        f'<span class="legend__key">'
        f'<span class="legend__swatch" style="background:{colours[name]}"></span>'
        f"{esc(name)}</span>"
        for name in categories
    )
    return f'<div class="legend">{keys}</div>'


def stacked(rows, categories, title: str, note: str = "", subtitle: str = "",
            share: bool = True, ordinal: bool = True) -> str:
    """One bar per row, split into `categories`; shares by default.

    `rows` is [(label, {category: value}), ...] in the order to draw. Totals
    ride the right-hand gutter so a share is never read without its
    denominator.

    A row whose values are None is a heading rather than a bar — the same
    convention hbar() uses, so a stratified chart reads the same way whichever
    of the two draws it.
    """
    rows = [(str(label), None if values is None else dict(values))
            for label, values in rows]
    # `ordinal` picks the palette: the single-hue ramp where the categories are
    # a sequence (therapy classes read as an era), the twelve-slot categorical
    # set where they are not (provider specialties).
    colours = ramp(categories) if ordinal else categorical(categories)
    if not any(values is not None for _l, values in rows):
        return _frame(40, f'<text x="0" y="24" font-size="12" fill="{INK_MUTED}" '
                          'font-family="DM Sans, sans-serif">No data.</text>',
                      title, note, subtitle)

    band = BAR_MAX + BAR_GAP
    # Size the label gutter to the longest row label rather than fixing it at
    # a width that happened to fit the first caller. At 66 units a label got
    # about ten characters, so "Ever prescribed (n=952)" rendered as
    # "bed (n=952)" — anchored at the right edge it lost its front and read as
    # a different word. Capped so the bars keep most of the width, and
    # anything past the cap is truncated rather than clipped, as hbar() does.
    top, bottom = 16, 22
    head_band = HEAD_BAND
    label_limit = int((STACK_LABEL_MAX - 14) / CHAR_W)
    rows = [(label if values is None else truncate(label, label_limit), values)
            for label, values in rows]
    # Headings are drawn across the full width, so they do not size the gutter.
    longest = max((len(label) for label, v in rows if v is not None), default=0)
    left = min(float(STACK_LABEL_MAX), max(66.0, longest * CHAR_W + 14))
    height = int(top + bottom + sum(head_band if v is None else band
                                    for _l, v in rows))
    plot_w = VIEW_W - left - VALUE_W
    axis_top = 100.0 if share else max(
        (sum(v.values()) for _l, v in rows if v is not None), default=1
    ) or 1
    ticks = ([0, 25, 50, 75, 100] if share else nice_ticks(axis_top)[0])
    if not share:
        axis_top = ticks[-1] or 1

    out = []
    for tick in ticks:
        x = left + plot_w * (tick / axis_top)
        out.append(f'<line x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" '
                   f'y2="{height - bottom + 2}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{x:.1f}" y="{height - bottom + 15}" font-size="10" '
                   f'fill="{INK_MUTED}" text-anchor="middle" '
                   'font-family="DM Mono, monospace" '
                   f'style="font-variant-numeric:tabular-nums">'
                   f'{tick_label(tick, ticks)}{"%" if share else ""}</text>')

    y = top
    for label, values in rows:
        if values is None:                    # a stratum heading, not a bar
            out.append(
                f'<text x="0" y="{y + HEAD_BASELINE:.1f}" font-size="10" '
                f'fill="{INK}" font-weight="600" '
                'font-family="DM Sans, sans-serif">'
                f"{esc(truncate(label, int(VIEW_W / CHAR_W)))}</text>"
            )
            y += head_band
            continue
        total = sum(values.values()) or 1
        x = left
        for name in categories:
            value = values.get(name, 0)
            if not value:
                continue
            scaled = (100.0 * value / total) if share else value
            width = plot_w * (scaled / axis_top)
            if width <= 0:
                continue
            # A 2px surface gap does the separating; never a stroke.
            drawn = max(width - SEGMENT_GAP, 0.5)
            out.append(f'<rect x="{x:.1f}" y="{y}" width="{drawn:.1f}" '
                       f'height="{BAR_MAX}" fill="{colours[name]}"/>')
            # The count inside the segment, where it fits. A stacked bar shows
            # shares; without the counts a reader has to multiply by the row
            # total to know whether a 30% slice is three patients or three
            # hundred. Skipped when the segment is too narrow to hold the
            # digits — a label overflowing into its neighbour would misattribute
            # the number, which is worse than omitting it. The row total in the
            # gutter still bounds anything left unlabelled.
            digits = f"{value:,.0f}"
            if drawn >= len(digits) * SEGMENT_CHAR_W + 6:
                out.append(
                    f'<text x="{x + drawn / 2:.1f}" '
                    f'y="{y + BAR_MAX * 0.74:.1f}" font-size="7.5" '
                    f'fill="{SURFACE}" text-anchor="middle" '
                    'font-family="DM Mono, monospace" '
                    'style="font-variant-numeric:tabular-nums">'
                    f"{digits}</text>"
                )
            x += width
        out.append(
            f'<text x="{left - 10}" y="{y + BAR_MAX * 0.78:.1f}" font-size="10" '
            f'fill="{INK}" text-anchor="end" font-family="DM Sans, sans-serif">'
            f"{esc(label)}</text>"
        )
        out.append(
            f'<text x="{left + plot_w + 8:.1f}" y="{y + BAR_MAX * 0.78:.1f}" '
            f'font-size="10" fill="{INK_MUTED}" '
            'font-family="DM Mono, monospace" '
            f'style="font-variant-numeric:tabular-nums">'
            f'n={sum(values.values()):,.0f}</text>'
        )
        y += band
    out.append(f'<line x1="{left}" y1="{top - 6}" x2="{left}" '
               f'y2="{height - bottom + 2}" stroke="{GRID}" stroke-width="1"/>')
    return (
        '<figure class="chart">'
        f"{caption(title)}"
        + (f'<p class="chart__subtitle">{esc(subtitle)}</p>' if subtitle else "")
        + legend(categories, colours)
        + f'<svg viewBox="0 0 {VIEW_W} {height}" width="100%" height="{height}" '
          'role="img" xmlns="http://www.w3.org/2000/svg">'
        + "".join(out) + "</svg>"
        + (f'<p class="chart__note">{esc(note)}</p>' if note else "")
        + "</figure>"
    )

# --------------------------------------------------------------------------
# pies — composition of a whole, one panel per group
# --------------------------------------------------------------------------
# A pie answers "what is this made of" and nothing else. It cannot be read for
# magnitude across panels — three pies of 109, 402 and 403,756 activations are
# drawn the same size — so every panel prints its own total, and the shares are
# printed beside the labels rather than left to the eye. Where the question is
# "which is bigger", stacked() is the form; this is for "what is the mix".
#
# *** Labels sit outside the circle on leaders, and there is no legend. ***
# A legend makes the reader bounce between a key and a wedge, twelve times per
# panel; a label written on the wedge either does not fit or lands on a colour
# it cannot be read against. Both problems disappear if the label is set on the
# surface outside the pie and a line joins it to its wedge. The leader is two
# segments — a connector out to the label's row, then a horizontal run into the
# text — so the eye arrives at the label along the baseline it will read, and
# there is exactly one corner to ignore.
#
# The panels stack rather than sitting side by side, and that is a consequence
# of the labels rather than a preference. Three panels across a 620-unit column
# leave about 45 units either side of each pie for text — ten characters, which
# turns "Hematology and Oncology" into "Hematolog…" and loses exactly the
# distinction the chart is drawn for. Full width per panel gives the labels
# room to be read.
PIE_R = 58                  # circle radius
PIE_STUB = 13               # how far past the circumference the bend sits
PIE_RUN = 34                # horizontal run from the bend to the text
PIE_LINE_H = 13             # minimum baseline gap between two labels on a side
PIE_LABEL_SIZE = 9
PIE_TITLE_BAND = 22         # the panel heading above each circle
PIE_MIN_SHARE = 2.0         # below this a slice folds into the residual
PIE_GAP = 10                # vertical air between panels


def _pie_points(cx: float, cy: float, r: float, angle: float):
    """The point at `angle` radians clockwise from twelve o'clock."""
    import math
    return cx + r * math.sin(angle), cy - r * math.cos(angle)


def _pie_fan(labels, low: float, high: float):
    """Push a side's labels apart so no two overlap, inside [low, high].

    Each label wants to sit at its wedge's mid-angle. Two narrow wedges next to
    each other want the same place, so the wants are honoured in order and each
    is moved down to clear the one before it; if that runs the last one past
    the bottom, the whole column shifts up by the overflow and is spread from
    the top instead. `labels` is [(desired_y, payload)]; the result keeps the
    payloads with their settled y.
    """
    ordered = sorted(labels, key=lambda item: item[0])
    settled, previous = [], None
    for want, payload in ordered:
        y = want if previous is None else max(want, previous + PIE_LINE_H)
        settled.append([y, payload])
        previous = y
    if settled and settled[-1][0] > high:
        shift = settled[-1][0] - high
        for row in settled:
            row[0] -= shift
        previous = None
        for row in settled:                      # re-clear the top edge
            row[0] = low if previous is None else max(row[0], previous + PIE_LINE_H)
            previous = row[0]
    return [(y, payload) for y, payload in settled]


def pies(panels, categories, title: str, note: str = "", subtitle: str = "",
         ordinal: bool = False, min_share: float = PIE_MIN_SHARE,
         residual: str = "Other", unit: str = "") -> str:
    """One pie per panel, labelled on elbow leaders, no legend.

    `panels` is [(label, {category: value}), ...]. `categories` fixes the
    colour assignment across every panel, so a hue means the same thing in all
    of them — the same contract stacked() has, and the reason the two can be
    read against each other.

    Slices under `min_share` percent fold into `residual`. Without that a pie
    of a dozen specialties spends most of its circumference on slivers too thin
    to point at, and the leader lines collide into a comb. The note should say
    the threshold; the caller knows what the slices are.
    """
    import math

    panels = [(str(label), dict(values)) for label, values in panels]
    colours = ramp(categories) if ordinal else categorical(categories)

    prepared = []
    for label, values in panels:
        total = sum(values.values())
        if not total:
            continue
        folded, rolled = {}, 0.0
        for name, value in values.items():
            if name != residual and 100.0 * value / total < min_share:
                rolled += value
            else:
                folded[name] = folded.get(name, 0) + value
        if rolled:
            folded[residual] = folded.get(residual, 0) + rolled
        # Largest first, clockwise from twelve — the pie convention, and it
        # puts the slice the reader wants where the eye lands first. The
        # residual goes last whatever its size: it is the leftover, and a pie
        # that opens on "Other" reads as though Other were the finding.
        ordered = sorted(
            folded.items(),
            key=lambda kv: (kv[0] == residual, -kv[1], str(kv[0]).lower()),
        )
        prepared.append((label, total, ordered))

    if not prepared:
        return _frame(40, f'<text x="0" y="24" font-size="12" fill="{INK_MUTED}" '
                          'font-family="DM Sans, sans-serif">No data.</text>',
                      title, note, subtitle)

    cx = VIEW_W / 2
    text_dx = PIE_R + PIE_STUB + PIE_RUN
    # Room for the text itself, from the end of the leader to the column edge.
    text_w = cx - text_dx - 8
    label_limit = max(int(text_w / (PIE_LABEL_SIZE * 0.62)) - 12, 8)

    out, y0 = [], 0
    for label, total, ordered in prepared:
        sides = {1: 0, -1: 0}
        for index, (_name, value) in enumerate(ordered):
            # Mid-angle of this wedge, to decide which side it labels on.
            before = sum(v for _n, v in ordered[:index])
            mid = 2 * math.pi * (before + value / 2) / total
            sides[1 if math.sin(mid) >= 0 else -1] += 1
        tallest = max(sides.values())
        body_h = max(2 * PIE_R + 16, tallest * PIE_LINE_H + 14)
        cy = y0 + PIE_TITLE_BAND + body_h / 2

        out.append(
            f'<text x="0" y="{y0 + 12}" font-size="10.5" fill="{INK}" '
            'font-family="DM Sans, sans-serif" font-weight="500">'
            f"{esc(label)}</text>"
        )
        out.append(
            f'<text x="{VIEW_W}" y="{y0 + 12}" font-size="9" '
            f'fill="{INK_MUTED}" text-anchor="end" '
            'font-family="DM Mono, monospace" '
            'style="font-variant-numeric:tabular-nums">'
            f'n={total:,.0f}{(" " + unit) if unit else ""}</text>'
        )

        pending = {1: [], -1: []}
        start = 0.0
        for name, value in ordered:
            share = value / total
            end = start + 2 * math.pi * share
            mid = (start + end) / 2
            colour = colours.get(name, OTHER)
            if share >= 0.999:                   # one category, whole circle
                out.append(
                    f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{PIE_R}" '
                    f'fill="{colour}" stroke="{SURFACE}" '
                    f'stroke-width="{SEGMENT_GAP}"/>'
                )
            else:
                x0_, y0_ = _pie_points(cx, cy, PIE_R, start)
                x1_, y1_ = _pie_points(cx, cy, PIE_R, end)
                large = 1 if (end - start) > math.pi else 0
                out.append(
                    f'<path d="M {cx:.1f} {cy:.1f} L {x0_:.1f} {y0_:.1f} '
                    f'A {PIE_R} {PIE_R} 0 {large} 1 {x1_:.1f} {y1_:.1f} Z" '
                    f'fill="{colour}" stroke="{SURFACE}" '
                    f'stroke-width="{SEGMENT_GAP}" stroke-linejoin="round"/>'
                )
            side = 1 if math.sin(mid) >= 0 else -1
            # *** The leader leaves the wedge at its most HORIZONTAL point, not
            # at its mid-angle. ***
            # A stub on the mid-angle points along the radius, which is to say
            # at the centre of the pie — and a label is never at the centre of
            # the pie. For a wedge pointing near twelve or six o'clock that
            # stub runs almost straight up or down, and the leader then needs a
            # right-angle turn to get out to the side and a second one to come
            # back to the label's row: two abrupt corners on a line whose whole
            # job is to be ignored.
            #
            # Clamping the anchor to three or nine o'clock — whichever side the
            # label is on — keeps it inside its own wedge, so it still points
            # at the right slice, while leaving the circumference already
            # heading towards the label. The 78% wedge anchors at three o'clock
            # instead of at 140 degrees, and its leader becomes a short
            # horizontal run. A wedge that never reaches the horizontal anchors
            # at whichever of its own edges comes closest, less a margin so the
            # line does not sit on the seam between two slices.
            #
            # The anchor's height is also what the label asks for, so a label
            # starts level with the point its leader leaves from.
            ideal = math.pi / 2 if side > 0 else 3 * math.pi / 2
            margin = min(0.06, (end - start) * 0.25)
            anchor = min(max(ideal, start + margin), end - margin)
            ax, ay = _pie_points(cx, cy, PIE_R, anchor)
            pending[side].append((ay, (name, value, share, ax, ay, colour)))
            start = end

        for side, items in pending.items():
            low = y0 + PIE_TITLE_BAND + 6
            high = y0 + PIE_TITLE_BAND + body_h - 6
            for settled_y, payload in _pie_fan(items, low, high):
                name, value, share, ax, ay, colour = payload
                tx = cx + side * text_dx
                elbow_x = tx - side * 8
                # Two segments, one bend: a connector from the wedge out to
                # the label's row, then a horizontal run into the text so the
                # eye arrives along the baseline it reads. The bend sits at
                # |x - cx| = PIE_R + PIE_STUB, outside the circumference, and
                # the connector only travels outward in x — the anchor is at
                # most PIE_R from the centre and the bend is past it on the
                # same side — so neither segment can re-enter the circle.
                #
                # There is no vertical segment any more. The label's row comes
                # from the anchor's own height, so it moves only where the fan
                # had to separate two labels, and then the connector takes up
                # the difference as a slope rather than as a second corner.
                turn_x = cx + side * (PIE_R + PIE_STUB)
                out.append(
                    f'<polyline points="{ax:.1f},{ay:.1f} '
                    f'{turn_x:.1f},{settled_y:.1f} '
                    f'{elbow_x:.1f},{settled_y:.1f}" '
                    f'fill="none" stroke="{GRID}" stroke-width="1"/>'
                )
                anchor = "start" if side > 0 else "end"
                digits = (f"{share * 100:.1f}%" if share < 0.1
                          else f"{share * 100:.0f}%")
                out.append(
                    f'<text x="{tx:.1f}" y="{settled_y:.1f}" '
                    f'font-size="{PIE_LABEL_SIZE}" fill="{INK}" '
                    f'text-anchor="{anchor}" dominant-baseline="middle" '
                    'font-family="DM Sans, sans-serif">'
                    f"{esc(truncate(str(name), label_limit))}"
                    f'<tspan fill="{INK_MUTED}" font-family="DM Mono, monospace" '
                    'style="font-variant-numeric:tabular-nums">'
                    f"  {digits} ({value:,.0f})</tspan></text>"
                )

        y0 += PIE_TITLE_BAND + body_h + PIE_GAP

    return _frame(int(y0 - PIE_GAP + 4), "".join(out), title, note, subtitle)


# --------------------------------------------------------------------------
# US states — a tile grid, one square per state
# --------------------------------------------------------------------------
# A schematic map, not a projection. Each state is one square placed roughly
# where it sits, which is the honest form for this data: the input is a phone
# area code resolved to a state, so the resolution *is* the state and drawing
# real borders would imply a precision the source does not have. It also needs
# no boundary geometry vendored into the repo, and every state gets the same
# area, so a small dense state is as readable as a large empty one.
#
# (row, column) on an 11x8 grid. Asserted complete and collision-free at import
# rather than trusted: a typo here silently moves a state or drops one.
US_TILES = {
    "AK": (0, 0),                                              "ME": (0, 10),
    "VT": (1, 9), "NH": (1, 10),
    "WI": (2, 4), "MI": (2, 6), "NY": (2, 8), "MA": (2, 9), "RI": (2, 10),
    "WA": (3, 0), "ID": (3, 1), "MT": (3, 2), "ND": (3, 3), "MN": (3, 4),
    "IL": (3, 5), "IN": (3, 6), "OH": (3, 7), "PA": (3, 8), "NJ": (3, 9),
    "CT": (3, 10),
    "OR": (4, 0), "NV": (4, 1), "WY": (4, 2), "SD": (4, 3), "IA": (4, 4),
    "MO": (4, 5), "KY": (4, 6), "WV": (4, 7), "VA": (4, 8), "MD": (4, 9),
    "DE": (4, 10),
    "CA": (5, 0), "UT": (5, 1), "CO": (5, 2), "NE": (5, 3), "KS": (5, 4),
    "AR": (5, 5), "TN": (5, 6), "NC": (5, 7), "SC": (5, 8), "DC": (5, 9),
    "AZ": (6, 1), "NM": (6, 2), "OK": (6, 3), "LA": (6, 4), "MS": (6, 5),
    "AL": (6, 6), "GA": (6, 7),
    "HI": (7, 0), "TX": (7, 3), "FL": (7, 7),
}
assert len(US_TILES) == 51, f"US_TILES has {len(US_TILES)} entries, expected 51"
assert len(set(US_TILES.values())) == 51, "US_TILES has two states in one cell"

TILE = 42                   # square side
TILE_GAP = 4
# The largest timeline marker's radius. Every other marker is this times
# sqrt(value / largest), which makes AREA proportional to the value.
MARKER_MAX_R = 5.5


def us_tiles(counts, title: str, note: str = "", subtitle: str = "",
             fmt=None, estimated=()) -> str:
    """A US state tile grid shaded by `counts` ({"PA": 6205, ...}).

    Shading is a five-step tint of the series colour over quintiles of the
    non-zero values, so the ramp encodes rank rather than magnitude — one org
    with ten times the facilities of the rest would otherwise flatten every
    other state to the palest step. A state with no value is drawn empty, which
    distinguishes "none here" from "the palest bucket".

    `estimated` names states whose count rests on a weaker claim than the rest
    — attributed at org level rather than per facility. Those tiles take a
    dashed border and a marked value, so the eye separates them without the
    reader having to hold the caveat in memory while reading the map. Two kinds
    of estimate on one grid needs the distinction drawn, not just noted.
    """
    fmt = fmt or (lambda value: f"{value:,.0f}")
    present = sorted(v for v in counts.values() if v)
    # Quintile edges over the non-zero values; ties collapse, which is correct.
    cuts = [present[min(len(present) - 1, int(len(present) * q / 5))]
            for q in range(1, 5)] if present else []
    tints = [0.18, 0.36, 0.54, 0.72, 1.0]

    def tint(value):
        if not value:
            return None
        step = sum(1 for c in cuts if value > c)
        return f"rgba({SERIES_RGB[0]}, {SERIES_RGB[1]}, {SERIES_RGB[2]}, " \
               f"{tints[min(step, len(tints) - 1)]:.2f})"

    rows = max(r for r, _c in US_TILES.values()) + 1
    cols = max(c for _r, c in US_TILES.values()) + 1
    band = TILE + TILE_GAP
    width = cols * band
    height = rows * band + 6
    out = []
    for state, (row, col) in sorted(US_TILES.items()):
        x, y = col * band, row * band
        value = counts.get(state, 0)
        fill = tint(value)
        soft = state in estimated and value
        out.append(
            f'<rect x="{x}" y="{y}" width="{TILE}" height="{TILE}" rx="3" '
            f'fill="{fill or SURFACE}" '
            f'stroke="{INK_MUTED if soft else GRID}" stroke-width="1"'
            + (' stroke-dasharray="3 2"' if soft else "")
            + "/>"
        )
        # The label sits on the tint, so it flips to the surface colour on the
        # two darkest steps where ink on violet would not clear contrast.
        dark = fill is not None and sum(1 for c in cuts if value > c) >= 3
        out.append(
            f'<text x="{x + TILE / 2:.0f}" y="{y + 17}" font-size="10" '
            f'fill="{SURFACE if dark else INK}" text-anchor="middle" '
            f'font-family="DM Sans, sans-serif" font-weight="600">'
            f"{esc(state)}</text>"
        )
        if value:
            out.append(
                f'<text x="{x + TILE / 2:.0f}" y="{y + 32}" font-size="9" '
                f'fill="{SURFACE if dark else INK_MUTED}" text-anchor="middle" '
                'font-family="DM Mono, monospace" '
                f'style="font-variant-numeric:tabular-nums">'
                f"{esc(fmt(value))}{'*' if soft else ''}</text>"
            )
    body = f'<g transform="translate({(VIEW_W - width) / 2:.0f}, 0)">' \
           f'{"".join(out)}</g>'
    return _frame(int(height), body, title, note, subtitle)


# --------------------------------------------------------------------------
# control chart — a proportion over time, against its limits (p-chart)
# --------------------------------------------------------------------------
def control_chart(points, title: str, note: str = "", subtitle: str = "",
                  sigma: float = 3.0, label_every: int = 6) -> str:
    """`points` is [(label, percent, numerator, denominator), ...] in order.

    A p-chart, with limits computed per period from that period's own
    denominator.

    The centreline is the POOLED proportion — total numerator over total
    denominator — and not the mean of the period percentages. Those differ
    whenever the denominators differ, and the average of percentages weights a
    five-encounter month as heavily as an eighty-nine-encounter one, which is
    how a quiet period ends up moving the line everyone reads.

    The spread is `sigma` * sqrt(p(1-p)/n) evaluated at each period's n, so the
    band breathes: wide where the period is thin, tight where it is not. This
    is the textbook p-chart and it is the right form whenever the denominator
    moves, which here it does by more than an order of magnitude. Holding one
    flat pair across that range calls a thin period special-cause for being
    thin — a month of one encounter at 0% sits outside any limit computed at a
    mean of forty, and says nothing. Both edges are clamped to 0-100, since a
    proportion cannot leave that range and a limit outside it is not a limit.

    A point outside the limits is drawn filled and ringed — the signal a
    control chart exists to show. Everything else is an open point.

    `label_every` thins the x labels; a tick is drawn at each labelled period
    so the label can be aligned to its own point rather than guessed at.
    """
    points = [(str(l), float(v), int(n), int(d)) for l, v, n, d in points]
    if not points:
        return _frame(40, f'<text x="0" y="24" font-size="12" fill="{INK_MUTED}" '
                          'font-family="DM Sans, sans-serif">No data.</text>',
                      title, note, subtitle)
    if len(points) < 2:
        return vbar([(l, v) for l, v, _n, _d in points], title, note)

    left, right, top, bottom = 46, 54, 18, 34
    plot_h, height = 150, 206
    plot_w = VIEW_W - left - right
    ticks, axis_top = [0, 25, 50, 75, 100], 100.0
    step = plot_w / (len(points) - 1)

    total_n = sum(n for _l, _v, n, _d in points)
    total_d = sum(d for _l, _v, _n, d in points) or 1
    centre = 100.0 * total_n / total_d
    p = centre / 100.0

    def limits(d):
        """The band at one period's denominator. A period with no denominator
        cannot be out of control, so its limits are the whole range."""
        if d <= 0:
            return 0.0, 100.0
        spread = sigma * (p * (1 - p) / d) ** 0.5 * 100.0
        return max(0.0, centre - spread), min(100.0, centre + spread)

    def y_of(value):
        return top + plot_h * (1 - value / axis_top)

    out = []
    for tick in ticks:
        y = y_of(tick)
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{VIEW_W - right}" '
                   f'y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{left - 8}" y="{y + 3.5:.1f}" font-size="10" '
                   f'fill="{INK_MUTED}" text-anchor="end" '
                   'font-family="DM Mono, monospace" '
                   f'style="font-variant-numeric:tabular-nums">{tick:.0f}%</text>')

    xs = [left + step * i for i in range(len(points))]
    band = [limits(d) for _l, _v, _n, d in points]
    # The in-control band as one filled shape, its two edges dashed over it.
    # Filled rather than drawn as two more series lines: what a reader needs is
    # whether a point is inside, not the exact height of either edge.
    upper = " ".join(f"L{x:.1f} {y_of(hi):.1f}" for x, (_lo, hi) in zip(xs, band))
    lower = " ".join(f"L{x:.1f} {y_of(lo):.1f}"
                     for x, (lo, _hi) in zip(reversed(xs), reversed(band)))
    out.append(f'<path d="M{xs[0]:.1f} {y_of(band[0][1]):.1f} {upper} {lower} Z" '
               f'fill="{SERIES}" fill-opacity="0.07"/>')
    for edge in (1, 0):
        d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y_of(b[edge]):.1f}"
                     for i, (x, b) in enumerate(zip(xs, band)))
        out.append(f'<path d="{d}" fill="none" stroke="{INK_MUTED}" '
                   'stroke-width="1" stroke-dasharray="2 3"/>')

    cy = y_of(centre)
    out.append(f'<line x1="{left}" y1="{cy:.1f}" x2="{VIEW_W - right}" '
               f'y2="{cy:.1f}" stroke="{INK}" stroke-width="1" '
               'stroke-dasharray="5 3"/>')
    # The limits move, so the gutter carries their range across the series
    # rather than one number that would read as constant.
    ucl_lo, ucl_hi = min(hi for _lo, hi in band), max(hi for _lo, hi in band)
    lcl_lo, lcl_hi = min(lo for lo, _hi in band), max(lo for lo, _hi in band)
    fmt_range = (lambda a, b: f"{a:.0f}%" if abs(a - b) < 0.5
                 else f"{a:.0f}\u2013{b:.0f}%")
    for label, value, dy in (
            ("mean", f"{centre:.0f}%", 0),
            ("UCL", fmt_range(ucl_lo, ucl_hi), y_of(ucl_hi) - cy - 6),
            ("LCL", fmt_range(lcl_lo, lcl_hi), y_of(lcl_lo) - cy + 6)):
        out.append(f'<text x="{VIEW_W - right + 6}" y="{cy + dy + 3.5:.1f}" '
                   f'font-size="9" fill="{INK if not dy else INK_MUTED}" '
                   f'font-family="DM Sans, sans-serif">{esc(label)}</text>')
        out.append(f'<text x="{VIEW_W - right + 6}" y="{cy + dy + 14:.1f}" '
                   f'font-size="9" fill="{INK_MUTED}" '
                   'font-family="DM Mono, monospace" '
                   f'style="font-variant-numeric:tabular-nums">{esc(value)}</text>')

    coords = [(x, y_of(v)) for x, (_l, v, _n, _d) in zip(xs, points)]
    out.append('<path d="' + "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in coords)
               + f'" fill="none" stroke="{SERIES}" stroke-width="1.75" '
               'stroke-linejoin="round" stroke-linecap="round"/>')
    signals = 0
    for (x, y), (_l, v, _n, d), (lo, hi) in zip(coords, points, band):
        outside = v > hi + 1e-9 or v < lo - 1e-9
        signals += outside
        out.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{4.2 if outside else 3.2}" '
            f'fill="{SERIES if outside else SURFACE}" stroke="{SERIES}" '
            f'stroke-width="{2 if outside else 1.5}"/>'
        )
        if outside:
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="none" '
                       f'stroke="{SERIES}" stroke-width="1"/>')

    # Every `label_every`-th period, with a tick at each so the label belongs to
    # a visible point rather than to a guess. The last period is not forced: at
    # 35 points a forced last label lands one step from the previous one and the
    # two collide, which is what a thinned axis is meant to prevent.
    baseline = top + plot_h
    every = max(1, int(label_every))
    for i, (label, _v, _n, _d) in enumerate(points):
        if i % every:
            continue
        out.append(f'<line x1="{xs[i]:.1f}" y1="{baseline:.1f}" '
                   f'x2="{xs[i]:.1f}" y2="{baseline + 5:.1f}" '
                   f'stroke="{INK_MUTED}" stroke-width="1"/>')
        anchor = ("start" if xs[i] - left < 14 else
                  "end" if VIEW_W - right - xs[i] < 14 else "middle")
        out.append(f'<text x="{xs[i]:.1f}" y="{baseline + 17}" '
                   f'font-size="9" fill="{INK_MUTED}" text-anchor="{anchor}" '
                   f'font-family="DM Sans, sans-serif">{esc(label)}</text>')
    if signals:
        out.append(f'<text x="{left}" y="{top + plot_h + 31}" font-size="9" '
                   f'fill="{INK}" font-family="DM Sans, sans-serif">'
                   f'{signals} point(s) outside the limits, ringed</text>')
    return _frame(height, "".join(out), title, note, subtitle)


# --------------------------------------------------------------------------
# timeline — items placed on a shared time axis, in labelled lanes
# --------------------------------------------------------------------------
# Timeline geometry, shared by the packer and the renderer. Compact: six lanes
# of twelve items each already pushed every chart past a printable page, and a
# figure taller than the page cannot honour break-inside:avoid — the renderer
# splits it and orphans the caption.
TIMELINE_PAD = (8, 8, 40)                   # left, right, top
TIMELINE_ROWS = (14, 13, 9, 28)             # lane label, row, lane gap, bottom


def _pack_timeline(lanes, span: float):
    """Lay out every lane's labels and return (packed rows, total height).

    Labels are packed greedily into sub-rows within each lane: an item's label
    goes in the first sub-row whose last label ends before this one starts.
    Split out from timeline() so a caller can ask what a set of lanes would
    measure BEFORE committing to drawing them — the plot is height-bound
    against a printed page, and the only way to keep the labels legible is to
    carry fewer items rather than to narrow the plot further.
    """
    left, right, top = TIMELINE_PAD
    lane_label_h, row_h, lane_gap, bottom = TIMELINE_ROWS
    plot_w = VIEW_W - left - right
    span = float(span) or 1.0
    packed, height = [], top
    for name, items in lanes:
        rows = []                      # rows[i] = x where that row is free again
        placed = []
        for item, at, low, high, weight in sorted(items, key=lambda r: r[1]):
            x = left + plot_w * min(max(at, 0.0), span) / span
            label = str(item)
            width = len(label) * CHAR_W + 10
            # The label sits right of the marker unless that would overflow.
            start = x + 7
            if start + width > VIEW_W - right:
                start = max(left, x - 7 - width)
            for index, free in enumerate(rows):
                if start >= free:
                    rows[index] = start + width
                    placed.append((index, x, start, label, at, low, high, weight))
                    break
            else:
                rows.append(start + width)
                placed.append((len(rows) - 1, x, start, label, at, low, high,
                               weight))
        packed.append((name, placed, len(rows)))
        height += lane_label_h + len(rows) * row_h + lane_gap
    return packed, height + bottom


def timeline_height(lanes, span: float = 72.0) -> int:
    """What timeline() would render these lanes at, in viewBox units."""
    lanes = [(str(name), list(items)) for name, items in lanes if items]
    return _pack_timeline(lanes, span)[1] if lanes else 40


def timeline(lanes, title: str, note: str = "", subtitle: str = "",
             span: float = 72.0, unit: str = "h", ticks=None,
             end_labels=(), scale: float = 0.8,
             page_aspect: float | None = None) -> str:
    """`lanes` is [(lane name, [(item, at, low, high, weight), ...]), ...].

    `at` is where the item sits on the axis and `low`/`high` its spread; a
    marker with a whisker, not a point, because "the median stay reaches this
    at 7h" is only interesting beside how tightly that holds. `weight` sizes
    the marker, so the eye finds the common items first.

    Labels are packed greedily into sub-rows within each lane: an item's label
    goes in the first sub-row whose last label ends before this one starts.
    Hospital events cluster hard in the first hours — a dozen items inside the
    first 90 minutes — so a single row per lane would overlap every label
    there, and dropping the ones that collide would silently hide the earliest
    part of the course, which is the part being asked about.
    """
    lanes = [(str(name), list(items)) for name, items in lanes if items]
    if not lanes:
        return _frame(40, f'<text x="0" y="24" font-size="12" fill="{INK_MUTED}" '
                          'font-family="DM Sans, sans-serif">No data.</text>',
                      title, note, subtitle)

    packed, height = _pack_timeline(lanes, span)
    left, right, top = TIMELINE_PAD
    lane_label_h, row_h, lane_gap, bottom = TIMELINE_ROWS
    plot_w = VIEW_W - left - right
    span = float(span) or 1.0
    heaviest = max((w for _n, items in lanes for *_r, w in items), default=1) or 1

    def x_of(value):
        return left + plot_w * min(max(value, 0.0), span) / span

    out = []
    # --- the axis, drawn once and referenced by every lane ----------------
    if ticks is None:
        ticks = [t for t in (0, 6, 12, 24, 36, 48, 60, 72, 96, 120) if t <= span]
        if ticks[-1] != span:
            ticks.append(span)
    ticks = [t for t in ticks if 0 <= t <= span]
    axis_y = height - bottom + 6
    for tick in ticks:
        x = x_of(tick)
        out.append(f'<line x1="{x:.1f}" y1="{top - 8}" x2="{x:.1f}" '
                   f'y2="{axis_y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{x:.1f}" y="{axis_y + 12:.1f}" font-size="9" '
                   f'fill="{INK_MUTED}" text-anchor="middle" '
                   'font-family="DM Mono, monospace" '
                   f'style="font-variant-numeric:tabular-nums">'
                   f'{tick:g}{esc(unit)}</text>')

    # What the two ends of the axis mean, named where a reader looks for them.
    # On a normalised axis the numbers alone are ambiguous — 0% and 100% of
    # what — and the answer is the whole point of normalising.
    if end_labels:
        first, last = end_labels[0], end_labels[-1]
        out.append(f'<text x="{left}" y="{top - 14:.1f}" font-size="9" '
                   f'fill="{INK_MUTED}" font-family="DM Sans, sans-serif">'
                   f'{esc(first)}</text>')
        out.append(f'<text x="{VIEW_W - right}" y="{top - 14:.1f}" '
                   f'font-size="9" fill="{INK_MUTED}" text-anchor="end" '
                   f'font-family="DM Sans, sans-serif">{esc(last)}</text>')

    y = top
    for name, placed, rows in packed:
        out.append(f'<text x="{left}" y="{y + 10:.1f}" font-size="10" '
                   f'fill="{INK}" font-weight="600" '
                   f'font-family="DM Sans, sans-serif">{esc(name)}</text>')
        y += lane_label_h
        for index, x, start, label, _at, low, high, weight in placed:
            row_y = y + index * row_h + row_h / 2
            if high > low:
                out.append(f'<line x1="{x_of(low):.1f}" y1="{row_y:.1f}" '
                           f'x2="{x_of(high):.1f}" y2="{row_y:.1f}" '
                           f'stroke="{SERIES}" stroke-width="1" '
                           'stroke-opacity="0.35"/>')
            # Area proportional to the value, so r scales with its square
            # root and nothing is added to it: r = R * sqrt(w / max) gives
            # area = pi * R^2 * w / max, which is linear in w. An additive
            # floor — the previous 2.2 + 2.6 * sqrt(...) — would break that,
            # and it is the classic way a bubble chart overstates its small
            # values. No floor is needed at these ranges: the lightest item
            # placed is a few percent of the heaviest, so its radius is still
            # a fifth of the largest rather than invisible.
            radius = MARKER_MAX_R * (weight / heaviest) ** 0.5
            out.append(f'<circle cx="{x:.1f}" cy="{row_y:.1f}" '
                       # Two decimals, not one: rounding a 1.74 radius to 1.7
                       # puts a 5% error into an area the reader is being
                       # asked to compare across marks.
                       f'r="{radius:.2f}" fill="{SERIES}" '
                       f'stroke="{SURFACE}" stroke-width="1"/>')
            anchor = "start" if start > x else "end"
            tx = start if anchor == "start" else start + len(label) * CHAR_W + 10
            out.append(f'<text x="{tx:.1f}" y="{row_y + 3.2:.1f}" '
                       f'font-size="9" fill="{INK}" text-anchor="{anchor}" '
                       f'font-family="DM Sans, sans-serif">{esc(label)}</text>')
        y += rows * row_h + lane_gap
    out.append(f'<line x1="{left}" y1="{axis_y:.1f}" x2="{VIEW_W - right}" '
               f'y2="{axis_y:.1f}" stroke="{GRID}" stroke-width="1"/>')
    # `page_aspect` is the printable height available to the plot divided by
    # the column width, so scale * (height / VIEW_W) <= page_aspect is exactly
    # "the plot fits the page". Narrowing until it does beats a fixed scale
    # that was right for five lanes and wrong for six: a lane added upstream
    # silently reintroduced the orphaned caption this guards against.
    if page_aspect and height:
        scale = min(scale, page_aspect * VIEW_W / height)
    return _frame(int(height), "".join(out), title, note, subtitle, scale)
