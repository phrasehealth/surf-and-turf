from app.pdf import markdown_to_html, render_report_pdf


def test_markdown_tables_render():
    html = markdown_to_html("T", "| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<table>" in html and "<th>a</th>" in html


def test_pdf_bytes():
    pdf = render_report_pdf("Test report", "## Section\n\nHello **world**.\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 1000


def test_printed_shell_has_cover_header_and_footer():
    """The shell is deterministic, so it is testable without a model or a warehouse."""
    import pypdfium2 as pdfium

    from app.report_shell import document_html
    from app.pdf import _BODY_CSS, html_to_pdf, markdown_to_body

    html = document_html(
        "Quarterly review", "What this covers.",
        [("Generated", "2026-09-17"), ("Source", "PENN")],
        markdown_to_body("## First analysis\n\nBody text.\n"), _BODY_CSS)
    pdf = html_to_pdf(html)
    assert pdf.startswith(b"%PDF-")

    doc = pdfium.PdfDocument(pdf)
    assert len(doc) == 2, "cover must occupy exactly one page"
    cover = doc[0].get_textpage().get_text_range()
    assert "Quarterly review" in cover and "What this covers." in cover
    assert "PENN" in cover, "server-supplied cover metadata is missing"
    assert "Internal use only".upper() in cover.upper(), "handling marking missing from cover"

    body = doc[1].get_textpage().get_text_range()
    assert "First analysis" in body
    assert "2 / 2" in body, "page counter missing from the footer"
    assert "Internal use only".upper() in body.upper()


def test_svg_chart_survives_markdown_conversion():
    """A chart written across several lines, with a blank line inside the markup.

    python-markdown does not treat `svg` as block-level, so without the
    extract/restore round-trip it parses the chart as inline HTML in a paragraph
    and a blank line splits it — the graphics disappear and the <text> children
    land in the body as loose paragraphs. Observed in a real report before the fix.
    """
    from app.pdf import markdown_to_body

    md = (
        "## Chart\n\n"
        '<svg viewBox="0 0 200 80" width="200" height="80" '
        'xmlns="http://www.w3.org/2000/svg">\n'
        '  <polyline points="0,60 50,40 100,50" fill="none" stroke="#4269d0"/>\n'
        "\n"                                     # the blank line that broke it
        '  <text x="0" y="75" font-size="8">2023-09</text>\n'
        "</svg>\n\n"
        "Body text after the chart.\n"
    )
    html = markdown_to_body(md)
    assert html.count("<svg") == 1 and html.count("</svg>") == 1, "svg was split"
    head = html.split("</svg>")[0]
    assert "<polyline" in head, "chart geometry was dropped"
    assert "<text" in head, "labels escaped the svg and became body text"
    assert "<p><svg" not in html, "svg wrapped in a paragraph"
    assert "Body text after the chart." in html


def test_two_charts_in_one_report_both_survive():
    from app.pdf import markdown_to_body

    one = '<svg viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'
    two = '<svg viewBox="0 0 20 20"><circle r="5"/></svg>'
    html = markdown_to_body(f"## A\n\n{one}\n\ntext\n\n## B\n\n{two}\n")
    assert html.count("<svg") == 2 and "<rect" in html and "<circle" in html
