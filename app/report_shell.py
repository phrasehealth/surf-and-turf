"""The printed shell every report shares: cover page, running header, footer.

Ported from the pharma-consulting reporting system (`scripts/orderset_report.py`
+ `scripts/report_style.css`), narrowed to the page furniture. The order-set
specific styling is not brought over: it is class-based markup this agent never
produces.

Everything here is deterministic and server-side. The model supplies a title, an
optional subtitle and the report body; it does not get a say in the cover, the
header, the footer or the handling marking. That is the point — a brand mark the
model can forget is not a brand mark.
"""
from __future__ import annotations

import html
from pathlib import Path

from .config import ROOT, settings

DESIGN_SYSTEM = ROOT / "assets" / "design_system"
TOKENS_CSS = DESIGN_SYSTEM / "colors_and_type.css"

# (width, height) in mm. The cover is full-bleed, so it needs a real height to
# push its meta block to the bottom of the sheet.
PAGE_MM = {
    "Letter": (215.9, 279.4),
    "A4": (210.0, 297.0),
    "Legal": (215.9, 355.6),
    "Ledger": (279.4, 431.8),
}


def page_height_mm(page_size: str) -> float:
    name, _, orientation = page_size.partition(" ")
    width, height = PAGE_MM.get(name, PAGE_MM["Letter"])
    return width if orientation.strip() == "landscape" else height


LOGO_MARK = (
    '<svg class="cover__mark" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">'
    '<rect width="64" height="64" rx="16" fill="#29103c"/>'
    '<rect x="14" y="17" width="36" height="5" rx="2.5" fill="#ffffff"/>'
    '<rect x="14" y="26" width="26" height="5" rx="2.5" fill="#ffffff"/>'
    '<rect x="14" y="35" width="32" height="5" rx="2.5" fill="#ffffff"/>'
    '<rect x="14" y="44" width="18" height="5" rx="2.5" fill="#ffffff"/>'
    "</svg>"
)

# Page furniture. Body typography lives in pdf.py, expressed in these tokens.
PAGE_CSS = """
@page {
  margin: 16mm 14mm 15mm 14mm;
  @top-left {
    content: string(running-doc);
    font-family: var(--font-sans); font-weight: var(--fw-medium); font-size: 8pt;
    letter-spacing: 0.06em; text-transform: uppercase; color: var(--primary-800);
    vertical-align: bottom; padding-bottom: 4mm;
  }
  @top-right {
    content: string(running-section);
    font-family: var(--font-sans); font-size: 8pt; color: var(--fg-muted);
    vertical-align: bottom; padding-bottom: 4mm;
  }
  @bottom-center {
    content: var(--handling-marking);
    font-family: var(--font-sans); font-weight: var(--fw-medium); font-size: 7pt;
    letter-spacing: 0.08em; text-transform: uppercase; color: var(--dark-600);
    vertical-align: top; padding-top: 4mm;
  }
  @bottom-left {
    content: "Phrase Health";
    font-family: var(--font-sans); font-size: 7.5pt; color: var(--fg-subtle);
    vertical-align: top; padding-top: 4mm;
  }
  @bottom-right {
    content: counter(page) " / " counter(pages);
    font-family: var(--font-mono); font-size: 7.5pt; color: var(--fg-subtle);
    vertical-align: top; padding-top: 4mm;
  }
}

/* The cover is the one place the brand gradient is allowed. */
@page cover {
  margin: 0;
  /* Spelled out: var() does not resolve inside @page. */
  background: linear-gradient(to bottom right, #E9DEFA 0%, #FFFFFF 55%, #FFF3E8 100%);
  @top-left { content: none; }
  @top-right { content: none; }
  @bottom-left { content: none; }
  @bottom-right { content: none; }
  /* Cleared too: a margin box needs a non-zero margin to paint into, and giving
     the cover one puts a seam across the gradient. The cover carries the
     marking as content instead -- see .cover__marking. */
  @bottom-center { content: none; }
}

*, *::before, *::after { box-sizing: border-box; }
body { background: none; }

/* Zero-height anchors whose only job is to publish the running-header strings. */
.doc-anchor { height: 0; overflow: hidden; }

.eyebrow {
  font-family: var(--font-sans); font-weight: var(--fw-medium); font-size: 7pt;
  text-transform: uppercase; letter-spacing: 0.08em; color: var(--fg-muted);
}

.cover {
  page: cover;
  break-after: page;
  position: relative;
  padding: 26mm 22mm 20mm 22mm;
}
.cover__mark { width: 44px; height: 44px; }
.cover__brand {
  margin-top: var(--sp-3); font-weight: var(--fw-bold); font-size: 12pt;
  color: var(--primary-950); letter-spacing: -0.01em;
}
.cover__title {
  margin-top: 22mm; font-family: var(--font-display); font-weight: var(--fw-bold);
  font-size: 32pt; line-height: 1.1; letter-spacing: -0.015em; color: var(--primary-950);
}
.cover__lede {
  margin-top: var(--sp-4); font-size: 11pt; line-height: var(--lh-relaxed);
  color: var(--fg-default); max-width: 105mm;
}
.cover__meta {
  position: absolute; left: 22mm; right: 22mm; bottom: 20mm;
  border-top: 1px solid var(--primary-200); padding-top: var(--sp-4);
  display: flex; flex-wrap: wrap; gap: var(--sp-6) var(--sp-10);
}
.cover__meta div { min-width: 34mm; }
.cover__meta dt { margin-bottom: 2px; }
.cover__meta dd {
  margin: 0; font-size: 10pt; font-weight: var(--fw-medium); color: var(--primary-950);
}
/* The marking every other page gets from @bottom-center. The cover is
   full-bleed, so it carries its own. */
/* Figures. Ported from pharma-consulting report_style.css — only the chart rules,
   which are the ones that apply to markup this system actually produces. */
.chart {
  margin: 14pt 0 16pt;
  break-inside: avoid;
}
.chart + .chart { margin-top: 18pt; }
.chart__title {
  font-family: var(--font-sans); font-weight: var(--fw-medium); font-size: 10pt;
  color: var(--primary-950); margin-bottom: 2pt; break-after: avoid;
}
.chart__id {
  font-family: var(--font-mono); font-size: 8.5pt; color: var(--fg-muted);
  margin-right: 8pt; letter-spacing: 0.02em;
}
.chart__subtitle {
  font-size: 9pt; color: var(--fg-muted); margin: 0 0 6pt; break-after: avoid;
}
.chart__note {
  font-size: 8.5pt; color: var(--fg-muted); margin: 5pt 0 0; line-height: var(--lh-normal);
}
.chart svg { display: block; max-width: 100%; }

.cover__marking {
  position: absolute; left: 22mm; right: 22mm; bottom: 8mm;
  text-align: center; font-family: var(--font-sans); font-weight: var(--fw-medium);
  font-size: 7pt; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--dark-600);
}
"""


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def render_cover(title: str, subtitle: str, meta: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<div><dt class="eyebrow">{esc(label)}</dt><dd>{esc(value)}</dd></div>'
        for label, value in meta if value
    )
    lede = f'<p class="cover__lede">{esc(subtitle)}</p>' if subtitle else ""
    return (
        '<section class="cover">'
        '<div class="doc-anchor"></div>'
        f"{LOGO_MARK}"
        '<div class="cover__brand">Phrase Health</div>'
        f'<h1 class="cover__title">{esc(title)}</h1>'
        f"{lede}"
        f'<dl class="cover__meta">{cells}</dl>'
        + (f'<div class="cover__marking">{esc(settings.report_marking)}</div>'
           if settings.report_marking else "")
        + "</section>"
    )


def document_html(title: str, subtitle: str, meta: list[tuple[str, str]],
                  body_html: str, body_css: str, page_size: str | None = None) -> str:
    """Wrap a rendered body in the shared page shell."""
    page_size = page_size or settings.report_page_size
    injected = (
        f"@page {{ size: {page_size}; }}\n"
        f".cover {{ height: {page_height_mm(page_size):g}mm; }}\n"
        f":root {{ --handling-marking: \"{settings.report_marking}\"; }}\n"
        # The document title rides the running header; each h2 republishes
        # itself into the right-hand slot.
        f'.doc-anchor {{ string-set: running-doc "{esc(title)}"; }}\n'
        "h2 { string-set: running-section content(text); }\n"
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{esc(title)}</title>"
        f'<link rel="stylesheet" href="{TOKENS_CSS.as_uri()}">'
        f"<style>{PAGE_CSS}{body_css}{injected}</style>"
        "</head><body>"
        f"{render_cover(title, subtitle, meta)}"
        f"{body_html}"
        "</body></html>"
    )
