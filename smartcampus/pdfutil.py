"""PDF generation (reports and receipts) with ReportLab."""
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _esc(v):
    return (str(v) if v is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def table_pdf(title, headers, rows, subtitle="", college=""):
    buf = io.BytesIO()
    wide = len(headers) > 5
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4) if wide else A4, title=title,
                            leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm, bottomMargin=14 * mm)
    st = getSampleStyleSheet()
    cell = st["BodyText"]
    cell.fontSize = 8
    cell.leading = 10
    story = [Paragraph(_esc(college), st["Heading3"]), Paragraph(_esc(title), st["Title"])]
    if subtitle:
        story.append(Paragraph(_esc(subtitle), st["Normal"]))
    story.append(Spacer(1, 6 * mm))
    data = [[Paragraph("<b>%s</b>" % _esc(h), cell) for h in headers]]
    for r in rows:
        data.append([Paragraph(_esc(c), cell) for c in r])
    if len(data) == 1:
        story.append(Paragraph("No records found.", st["Normal"]))
    else:
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbe7f5")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f8fb")]),
        ]))
        story.append(t)
    doc.build(story)
    return buf.getvalue()


def receipt_pdf(d, college=""):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Receipt " + d["receipt_no"],
                            leftMargin=20 * mm, rightMargin=20 * mm, topMargin=20 * mm, bottomMargin=20 * mm)
    st = getSampleStyleSheet()
    story = [Paragraph(_esc(college), st["Heading2"]), Paragraph("PAYMENT RECEIPT", st["Title"]), Spacer(1, 6 * mm)]
    rows = [
        ["Receipt No.", d["receipt_no"]],
        ["Date", d["paid_on"]],
        ["Student", "%s (%s)" % (d["student_name"], d["student_no"])],
        ["Fee type", d["fee_type"]],
        ["Amount paid", "%.2f" % d["amount"]],
        ["Payment method", d["method"]],
        ["Reference", d["reference"] or "-"],
        ["Fee total", "%.2f" % d["fee_amount"]],
        ["Total paid to date", "%.2f" % d["paid_total"]],
        ["Outstanding balance", "%.2f" % d["balance"]],
        ["Received by", d["staff_name"]],
    ]
    t = Table([[_esc(a), _esc(b)] for a, b in rows], colWidths=[55 * mm, 105 * mm])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef3fa")),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    story += [t, Spacer(1, 10 * mm),
              Paragraph("This is a computer-generated receipt issued by Smart Campus.", st["Italic"])]
    doc.build(story)
    return buf.getvalue()
