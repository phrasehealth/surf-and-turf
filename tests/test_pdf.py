from app.pdf import markdown_to_html, render_report_pdf


def test_markdown_tables_render():
    html = markdown_to_html("T", "| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<table>" in html and "<th>a</th>" in html


def test_pdf_bytes():
    pdf = render_report_pdf("Test report", "## Section\n\nHello **world**.\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 1000
