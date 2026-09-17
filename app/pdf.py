"""Markdown -> styled HTML -> PDF. Deterministic; the model never touches PDF bytes."""
from __future__ import annotations

import html
import os
import sys
from datetime import datetime
from pathlib import Path

import markdown

_CSS = """
@page { size: Letter; margin: 0.9in 0.8in 0.9in 0.8in;
        @bottom-center { content: counter(page) " / " counter(pages); font-size: 9pt; color: #777; } }
body { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 10.5pt;
       line-height: 1.45; color: #1b1f24; }
h1 { font-size: 22pt; margin: 0 0 4pt 0; }
h2 { font-size: 15pt; margin: 20pt 0 6pt; border-bottom: 1px solid #d9dde3; padding-bottom: 3pt; }
h3 { font-size: 12pt; margin: 14pt 0 4pt; }
.meta { color: #6b7280; font-size: 9.5pt; margin-bottom: 18pt; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0 12pt; font-size: 9.5pt;
        page-break-inside: auto; }
thead { display: table-header-group; }
th, td { border: 1px solid #d9dde3; padding: 4pt 6pt; text-align: left; vertical-align: top; }
th { background: #f3f4f6; font-weight: 600; }
tr { page-break-inside: avoid; }
code, pre { font-family: Menlo, Consolas, monospace; font-size: 9pt; background: #f6f8fa; }
pre { padding: 8pt; border-radius: 4pt; white-space: pre-wrap; }
blockquote { border-left: 3px solid #d9dde3; margin: 8pt 0; padding: 2pt 10pt; color: #4b5563; }
img { max-width: 100%; }
"""


def markdown_to_html(title: str, body_md: str, author: str | None = None) -> str:
    body = markdown.markdown(
        body_md,
        extensions=["tables", "fenced_code", "sane_lists", "toc", "attr_list"],
        output_format="html5",
    )
    meta = datetime.now().strftime("%B %d, %Y")
    if author:
        meta += f" &middot; {html.escape(author)}"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>"
        f"<h1>{html.escape(title)}</h1><div class='meta'>{meta}</div>{body}</body></html>"
    )


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


def html_to_pdf(html_doc: str) -> bytes:
    _ensure_native_libs()
    from weasyprint import HTML  # heavy import, keep it lazy

    return HTML(string=html_doc).write_pdf()


def render_report_pdf(title: str, body_md: str, author: str | None = None) -> bytes:
    return html_to_pdf(markdown_to_html(title, body_md, author))
