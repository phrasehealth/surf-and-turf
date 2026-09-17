"""Markdown -> styled HTML -> PDF. Deterministic; the model never touches PDF bytes.

The body typography here is expressed in the design-system tokens loaded by
`report_shell`, so a brand change is one stylesheet, not an edit in this file.
The page furniture — cover, running header, footer — lives in `report_shell`.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import markdown

from .report_shell import DESIGN_SYSTEM, document_html

# Body styling only. Every value is a design-system token; nothing here invents
# a colour or a font.
_BODY_CSS = """
html { font-size: 9pt; }
body {
  color: var(--fg-default);
  font-family: var(--font-sans);
  font-size: 10pt;
  line-height: var(--lh-normal);
}
h1 { font-family: var(--font-display); font-weight: var(--fw-bold);
     font-size: 20pt; margin: 0 0 4pt 0; color: var(--primary-950); }
h2 { font-family: var(--font-display); font-weight: var(--fw-bold);
     font-size: 14pt; margin: 18pt 0 6pt; padding-bottom: 3pt;
     border-bottom: 1px solid var(--primary-200); color: var(--primary-950);
     break-after: avoid; }
h3 { font-weight: var(--fw-medium); font-size: 11pt; margin: 14pt 0 4pt;
     color: var(--primary-900, var(--primary-950)); break-after: avoid; }
p { margin: 0 0 6pt; }
.meta { color: var(--fg-muted); font-size: 9pt; margin-bottom: 14pt; }

table { border-collapse: collapse; width: 100%; margin: 8pt 0 12pt;
        font-size: 9pt; break-inside: auto; }
thead { display: table-header-group; }
th, td { border: 1px solid var(--primary-200); padding: 4pt 6pt;
         text-align: left; vertical-align: top; }
th { background: var(--primary-50, #f3f4f6); font-weight: var(--fw-medium);
     color: var(--primary-950); }
td.num, th.num { text-align: right; font-family: var(--font-mono); }
tr { break-inside: avoid; }

code, pre { font-family: var(--font-mono); font-size: 8.5pt; }
pre { background: var(--primary-50, #f6f8fa); padding: 8pt; border-radius: 4pt;
      white-space: pre-wrap; break-inside: avoid; }
blockquote { border-left: 3px solid var(--primary-200); margin: 8pt 0;
             padding: 2pt 10pt; color: var(--fg-muted); }
ul, ol { margin: 0 0 6pt; padding-left: 16pt; }
li { margin-bottom: 2pt; }
img, svg { max-width: 100%; }
hr { border: 0; border-top: 1px solid var(--primary-200); margin: 14pt 0; }
"""


def _ensure_native_libs() -> None:
    """Let WeasyPrint find Homebrew's pango/glib on macOS.

    WeasyPrint dlopen()s libgobject/libpango by bare name; on macOS those live
    under the Homebrew prefix, which is not on the default search path, so the
    import fails with "cannot load library 'libgobject-2.0-0'".  Linux and the
    container image install them system-wide, so this is a no-op there.
    """
    if sys.platform != "darwin":
        return
    var = "DYLD_FALLBACK_LIBRARY_PATH"
    current = os.environ.get(var, "")
    for prefix in ("/opt/homebrew/lib", "/usr/local/lib"):  # arm64, then intel
        if Path(prefix, "libgobject-2.0.dylib").exists() and prefix not in current.split(":"):
            os.environ[var] = f"{current}:{prefix}".lstrip(":")
            current = os.environ[var]


def markdown_to_body(body_md: str) -> str:
    """The report body only. Title and metadata are the cover's job."""
    return markdown.markdown(
        body_md,
        extensions=["tables", "fenced_code", "sane_lists", "toc", "attr_list"],
        output_format="html5",
    )


def html_to_pdf(html_doc: str) -> bytes:
    _ensure_native_libs()
    from weasyprint import HTML  # heavy import, keep it lazy

    # base_url resolves the stylesheet's relative font URLs. Nothing is fetched
    # over the network: the fonts ship in the image beside the stylesheet.
    return HTML(string=html_doc, base_url=str(DESIGN_SYSTEM) + "/").write_pdf()


def render_report_pdf(title: str, body_md: str, author: str | None = None,
                      subtitle: str = "", meta: list[tuple[str, str]] | None = None) -> bytes:
    if meta is None:
        meta = [("Generated", datetime.now().strftime("%Y-%m-%d"))]
        if author:
            meta.append(("Prepared for", author))
    return html_to_pdf(
        document_html(title, subtitle, meta, markdown_to_body(body_md), _BODY_CSS)
    )


# Kept for callers that want the body HTML with a heading, e.g. a future preview.
def markdown_to_html(title: str, body_md: str, author: str | None = None) -> str:
    import html as _html
    meta = datetime.now().strftime("%B %d, %Y")
    if author:
        meta += f" &middot; {_html.escape(author)}"
    return (f"<h1>{_html.escape(title)}</h1><div class='meta'>{meta}</div>"
            + markdown_to_body(body_md))
