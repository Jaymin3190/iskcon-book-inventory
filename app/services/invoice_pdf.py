from html import escape
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .timezone import format_ist_datetime


def build_invoice_pdf(invoice, items):
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title=f"Invoice {invoice['invoice_number']}",
    )
    styles = getSampleStyleSheet()
    styles["Title"].textColor = colors.HexColor("#242a33")
    styles["Title"].fontSize = 21
    styles["Heading2"].textColor = colors.HexColor("#e71936")
    styles["BodyText"].fontSize = 9

    def money(cents):
        return f"INR {cents / 100:,.2f}"

    subtotal_cents = sum(round(float(item["line_total"]) * 100) for item in items)
    discount_cents = round(float(invoice["discount"] or 0) * 100)
    allocated_discount = 0
    line_rows = [[
        "#", "Item name", "HSN/SAC", "Quantity", "Price / Unit",
        "Discount", "Taxable / Unit", "Amount",
    ]]
    for index, item in enumerate(items):
        gross_cents = round(float(item["line_total"]) * 100)
        if index == len(items) - 1:
            line_discount = discount_cents - allocated_discount
        elif subtotal_cents:
            line_discount = round(discount_cents * gross_cents / subtotal_cents)
            line_discount = min(gross_cents, max(0, line_discount))
        else:
            line_discount = 0
        allocated_discount += line_discount
        quantity = int(item["qty"])
        net_cents = gross_cents - line_discount
        taxable_unit_cents = round(net_cents / quantity) if quantity else 0
        line_rows.append([
            str(index + 1),
            Paragraph(escape(str(item["title"])), styles["BodyText"]),
            "49011010",
            str(quantity),
            money(round(float(item["unit_price"]) * 100)),
            money(line_discount),
            money(taxable_unit_cents),
            money(net_cents),
        ])

    line_rows.append([
        "",
        "Total",
        "",
        str(sum(int(item["qty"]) for item in items)),
        "",
        money(discount_cents),
        "",
        money(round(float(invoice["total"]) * 100)),
    ])
    widths = [9 * mm, 74 * mm, 21 * mm, 19 * mm, 31 * mm, 29 * mm, 31 * mm, 32 * mm]
    table = Table(line_rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e71936")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f5e4e6")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#888888")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    story = [
        Paragraph("ISKCON JAMNAGAR BOOKS &amp; GIFTS", styles["Title"]),
        Paragraph("GSTIN: 24AAATI0017P9ZL &nbsp;&nbsp; | &nbsp;&nbsp; State: 24-Gujarat", styles["BodyText"]),
        Spacer(1, 5 * mm),
        Paragraph("Tax Invoice", styles["Heading2"]),
    ]
    details = [
        [
            Paragraph(
                f"<b>Bill To:</b> {escape(str(invoice['customer_name'] or '-'))}<br/>"
                f"<b>Contact:</b> {escape(str(invoice['customer_phone'] or '-'))}",
                styles["BodyText"],
            ),
            Paragraph(
                f"<b>Invoice No.:</b> {escape(str(invoice['invoice_number']))}<br/>"
                f"<b>Date (IST):</b> {escape(format_ist_datetime(invoice['created_at']))}<br/>"
                f"<b>Place of Supply:</b> 24-Gujarat<br/>"
                f"<b>Distributor:</b> {escape(str(invoice['cashier_name'] or '-'))}",
                styles["BodyText"],
            ),
        ]
    ]
    details_table = Table(details, colWidths=[110 * mm, 110 * mm], hAlign="LEFT")
    details_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.extend([details_table, Spacer(1, 3 * mm), table, Spacer(1, 4 * mm)])
    payment_label = "razorpay" if invoice["razorpay_qr_code_id"] else invoice["payment_mode"]
    payment = str(payment_label or "").upper()
    if invoice["payment_ref"]:
        payment += f" ({invoice['payment_ref']})"
    story.append(Paragraph(f"<b>Payment:</b> {escape(payment)}", styles["BodyText"]))
    document.build(story)
    return buffer.getvalue()
