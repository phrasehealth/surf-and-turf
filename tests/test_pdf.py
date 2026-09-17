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
