import re
import logging
import sqlite3
from html import escape
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from ..auth import require_roles
from ..database import get_db
from ..services.audit import log_audit
from ..services.invoice_pdf import build_invoice_pdf
from ..services.ledger import check_available, move_stock
from ..services.razorpay import (
    RazorpayError,
    close_qr_code,
    create_qr_code,
    is_configured as razorpay_is_configured,
)
from ..services.scope import invoice_location_ids, location_ids
from ..services.timezone import format_ist_date

router = APIRouter(prefix="/api/pos", tags=["pos"])

PAYMENT_MODES = ("cash", "upi", "razorpay")
logger = logging.getLogger(__name__)


def next_invoice_number(db, user) -> str:
    """Simple per-user series such as INV-superadmin-0001."""
    seq = db.execute(
        """INSERT INTO invoice_sequences (user_id, next_number) VALUES (?, 1)
           ON CONFLICT(user_id) DO UPDATE SET next_number = next_number + 1
           RETURNING next_number""",
        (user["id"],),
    ).fetchone()["next_number"]
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(user["username"] or "")).strip("-._")
    return f"INV-{name or ('user' + str(user['id']))}-{seq:04d}"


class InvoiceItemIn(BaseModel):
    barcode: str
    qty: int = 1


class InvoiceIn(BaseModel):
    location_id: Optional[int] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    payment_mode: str = "cash"
    payment_ref: Optional[str] = None
    discount: float = 0
    items: list[InvoiceItemIn]


@router.get("/invoices")
def list_invoices(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    ids = invoice_location_ids(db, user)
    sql = """SELECT i.*, l.code AS location_code, l.name AS location_name, u.name AS cashier_name
             FROM invoices i
             JOIN locations l ON l.id = i.location_id
             LEFT JOIN users u ON u.id = i.cashier_id
             WHERE i.location_id IN ({})""".format(",".join("?" * len(ids)))
    params = list(ids)
    if user["role"] == "distributor":
        sql += " AND i.cashier_id = ?"
        params.append(user["id"])
    sql += " ORDER BY i.id DESC"
    return [dict(r) for r in db.execute(sql, params).fetchall()]


@router.get("/invoices/export")
def export_invoices(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    ids = invoice_location_ids(db, user)
    sql = """SELECT i.id, i.invoice_number, i.created_at,
                    l.name AS location_name,
                    u.name AS cashier_name, u.username AS cashier_username,
                    i.customer_name, i.customer_phone,
                    CASE WHEN i.razorpay_qr_code_id IS NOT NULL THEN 'razorpay'
                         ELSE i.payment_mode END AS payment_mode,
                    i.payment_ref, i.status, i.subtotal, i.discount, i.total,
                    b.title AS book_title, b.barcode,
                    ii.qty, ii.unit_price, ii.line_total
             FROM invoices i
             JOIN locations l ON l.id = i.location_id
             LEFT JOIN users u ON u.id = i.cashier_id
             LEFT JOIN invoice_items ii ON ii.invoice_id = i.id
             LEFT JOIN books b ON b.id = ii.book_id
             WHERE i.location_id IN ({})""".format(",".join("?" * len(ids)))
    params = list(ids)
    if user["role"] == "distributor":
        sql += " AND i.cashier_id = ?"
        params.append(user["id"])
    sql += " ORDER BY i.id DESC, ii.id"
    return [dict(row) for row in db.execute(sql, params).fetchall()]


@router.post("/invoices")
def create_invoice(data: InvoiceIn, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    if not data.items:
        raise HTTPException(400, "Cart is empty")
    if data.payment_mode not in PAYMENT_MODES:
        raise HTTPException(400, f"Invalid payment mode. Valid: {', '.join(PAYMENT_MODES)}")
    if data.payment_mode == "razorpay" and not razorpay_is_configured():
        raise HTTPException(503, "Razorpay is not configured")
    customer_name = (data.customer_name or "").strip()
    customer_phone = (data.customer_phone or "").strip()
    if not customer_name:
        raise HTTPException(400, "Customer name is required")
    if not customer_phone:
        raise HTTPException(400, "Phone number is required")
    if user["role"] != "super_admin":
        loc_id = user["zone_id"]
        if not loc_id or (data.location_id is not None and data.location_id != loc_id):
            raise HTTPException(403, "Users can only bill against their assigned zone")
    else:
        loc_id = data.location_id or user["zone_id"]
        if not loc_id:
            warehouse = db.execute(
                "SELECT id FROM locations WHERE type = 'warehouse' AND parent_id IS NULL ORDER BY id LIMIT 1"
            ).fetchone()
            loc_id = warehouse["id"] if warehouse else None
    if not loc_id or loc_id not in location_ids(db, user):
        raise HTTPException(400, "You have no billing location")
    loc = db.execute("SELECT * FROM locations WHERE id = ?", (loc_id,)).fetchone()

    resolved_by_book = {}
    for it in data.items:
        if it.qty <= 0:
            raise HTTPException(400, "Quantities must be positive")
        book = db.execute("SELECT * FROM books WHERE barcode = ? OR isbn = ?", (it.barcode, it.barcode)).fetchone()
        if not book:
            raise HTTPException(400, f"Unknown barcode: {it.barcode}")
        if book["id"] in resolved_by_book:
            existing_book, existing_qty = resolved_by_book[book["id"]]
            resolved_by_book[book["id"]] = (existing_book, existing_qty + it.qty)
        else:
            resolved_by_book[book["id"]] = (book, it.qty)
    resolved = list(resolved_by_book.values())
    for book, qty in resolved:
        if not check_available(db, book["id"], loc_id, qty):
            raise HTTPException(400, f"Insufficient stock for '{book['title']}' at {loc['name']}")

    subtotal = sum(b["price"] * q for b, q in resolved)
    discount = min(data.discount, subtotal)
    total = round(subtotal - discount, 2)
    if data.payment_mode == "razorpay" and total <= 0:
        raise HTTPException(400, "Razorpay payments must have a positive total")

    qr_code = None
    try:
        num = next_invoice_number(db, user)
        status = "pending" if data.payment_mode == "razorpay" else "paid"
        stored_payment_mode = "upi" if data.payment_mode == "razorpay" else data.payment_mode
        cur = db.execute(
            """INSERT INTO invoices (invoice_number, location_id, customer_name, customer_phone,
                                     payment_mode, payment_ref, status, subtotal, discount, total, cashier_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (num, loc_id, customer_name, customer_phone,
             stored_payment_mode, data.payment_ref, status, subtotal, discount, total, user["id"]),
        )
        iid = cur.lastrowid
        for book, q in resolved:
            db.execute(
                "INSERT INTO invoice_items (invoice_id, book_id, qty, unit_price, line_total) VALUES (?,?,?,?,?)",
                (iid, book["id"], q, book["price"], round(book["price"] * q, 2)),
            )
            move_stock(
                db, book["id"], loc_id, -q, "SALE_OUT", "invoices", iid, user["id"], num,
                commit=False,
            )
        if data.payment_mode == "razorpay":
            qr_code = create_qr_code(num, total)
            db.execute(
                """UPDATE invoices
                      SET razorpay_qr_code_id = ?, razorpay_qr_image_url = ?
                    WHERE id = ?""",
                (qr_code["id"], qr_code["image_url"], iid),
            )
        db.commit()
    except RazorpayError as error:
        db.rollback()
        raise HTTPException(502, str(error)) from error
    except sqlite3.Error:
        db.rollback()
        if qr_code:
            try:
                close_qr_code(qr_code["id"])
            except RazorpayError as error:
                logger.error("Could not close orphan Razorpay QR %s: %s", qr_code["id"], error)
        raise

    payment_label = "razorpay" if data.payment_mode == "razorpay" else data.payment_mode
    log_audit(db, user["id"], "INVOICE_CREATE", "invoices", iid, f"{num} total {total} {payment_label} {status}", user["username"])
    result = {
        "invoice_id": iid,
        "invoice_number": num,
        "total": total,
        "subtotal": subtotal,
        "discount": discount,
        "status": status,
        "payment_mode": payment_label,
    }
    if qr_code:
        result.update(
            razorpay_qr_code_id=qr_code["id"],
            razorpay_qr_image_url=qr_code["image_url"],
        )
    return result


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    inv = db.execute(
        """SELECT i.*, l.code AS location_code, l.name AS location_name, u.name AS cashier_name
           FROM invoices i
           JOIN locations l ON l.id = i.location_id
           LEFT JOIN users u ON u.id = i.cashier_id
           WHERE i.id = ?""",
        (invoice_id,),
    ).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["location_id"] not in invoice_location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    if user["role"] == "distributor" and inv["cashier_id"] != user["id"]:
        raise HTTPException(403, "Distributors can only access their own bills")
    items = db.execute(
        """SELECT ii.*, b.title, b.barcode
           FROM invoice_items ii JOIN books b ON b.id = ii.book_id
           WHERE ii.invoice_id = ?""",
        (invoice_id,),
    ).fetchall()
    data = dict(inv)
    data["items"] = [dict(r) for r in items]
    return data


def invoice_date_ist(value):
    return format_ist_date(value)


def invoice_html(inv, items):
    subtotal_cents = sum(round(float(it["line_total"]) * 100) for it in items)
    discount_cents = round(float(inv["discount"] or 0) * 100)
    line_discounts = []
    allocated_cents = 0
    for index, item in enumerate(items):
        line_cents = round(float(item["line_total"]) * 100)
        if index == len(items) - 1:
            line_discount = discount_cents - allocated_cents
        elif subtotal_cents:
            line_discount = round(discount_cents * line_cents / subtotal_cents)
            line_discount = min(line_cents, max(0, line_discount))
        else:
            line_discount = 0
        line_discounts.append(line_discount)
        allocated_cents += line_discount

    def money(cents):
        return f"₹ {cents / 100:,.2f}"

    rows = []
    for index, item in enumerate(items):
        gross_cents = round(float(item["line_total"]) * 100)
        line_discount = line_discounts[index]
        net_cents = gross_cents - line_discount
        discount_percent = (line_discount / gross_cents * 100) if gross_cents else 0
        unit_cents = round(float(item["unit_price"]) * 100)
        qty = int(item["qty"])
        taxable_unit = net_cents / qty if qty else 0
        rows.append(
            "<tr>"
            f"<td>{index + 1}</td>"
            f"<td class='item-name'>{escape(str(item['title']))}</td>"
            "<td>49011010</td>"
            f"<td class='r'>{qty}</td>"
            f"<td class='r'>{money(unit_cents)}</td>"
            f"<td class='r'>{money(line_discount)}<span class='percent'>({discount_percent:.3f}%)</span></td>"
            f"<td class='r'>{money(round(taxable_unit))}</td>"
            f"<td class='r'>{money(round(taxable_unit))}</td>"
            f"<td class='r'>{money(net_cents)}</td>"
            "</tr>"
        )
    total_qty = sum(int(item["qty"]) for item in items)
    rows_html = "".join(rows)
    customer_name = escape(str(inv["customer_name"] or "-"))
    customer_phone = escape(str(inv["customer_phone"] or "-"))
    distributor = escape(str(inv["cashier_name"] or "-"))
    invoice_number = escape(str(inv["invoice_number"]))
    date = invoice_date_ist(inv["created_at"])
    payment_ref = escape(str(inv["payment_ref"] or ""))
    payment_label = "razorpay" if inv["razorpay_qr_code_id"] else inv["payment_mode"]
    payment = escape(str(payment_label or "").upper())
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Invoice {inv['invoice_number']}</title>
    <style>
      * {{ box-sizing: border-box; }}
      body {{ margin: 0; color: #171717; font-family: Arial, sans-serif; font-size: 12px; }}
      .contact {{ min-height: 76px; padding: 18px 22px 18px 20%; display: grid; grid-template-columns: 1fr 1.3fr 1fr; gap: 16px; align-items: center; background: #e71936; color: #fff; }}
      .contact div {{ border-left: 2px solid rgba(255,255,255,.85); padding-left: 10px; }}
      .brand {{ width: 60%; min-height: 98px; margin-top: -1px; padding: 14px 18px; border-radius: 0 0 70px 0; background: #242a33; color: #fff; }}
      .brand h1 {{ margin: 0 0 12px; font-size: 23px; font-weight: 400; }}
      .brand p {{ margin: 4px 0; color: #eee; }}
      .details {{ display: grid; grid-template-columns: 1.3fr 1fr; gap: 26px; padding: 16px 4px 20px; }}
      .bill-to h2 {{ margin: 0 0 10px; color: #e71936; font-size: 16px; font-weight: 400; }}
      .customer {{ margin-bottom: 10px; font-size: 23px; font-weight: 700; }}
      .bill-to p {{ margin: 6px 0; }}
      .invoice-info h2 {{ margin: 0 0 14px; font-size: 30px; font-weight: 400; }}
      .invoice-info p {{ display: grid; grid-template-columns: 180px 1fr; gap: 8px; margin: 7px 0; }}
      table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
      th, td {{ border: 2px solid #888; padding: 7px 5px; text-align: left; overflow-wrap: anywhere; }}
      th {{ border-color: #fff; background: #e71936; color: #fff; font-size: 12px; }}
      td {{ font-size: 11px; }}
      .r {{ text-align: right; }}
      .item-name {{ font-weight: 700; }}
      .percent {{ display: block; white-space: nowrap; }}
      .total td {{ background: #e71936; color: #fff; font-weight: 700; }}
      @page {{ size: A4 landscape; margin: 8mm; }}
      @media print {{ body {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }} }}
    </style></head><body>
    <div class="contact">
      <div>☎ &nbsp;9427272312</div>
      <div>✉ &nbsp;iskconjamnagar@gmail.com</div>
      <div>⌖ &nbsp;Behind Dhir Toyota Jamnagar-Rajkot Highway</div>
    </div>
    <div class="brand">
      <h1>ISKCON JAMNAGAR BOOKS &amp; GIFTS</h1>
      <p>GSTIN: 24AAATI0017P9ZL</p>
      <p>State: 24-Gujarat</p>
    </div>
    <section class="details">
      <div class="bill-to">
        <h2>Bill To:</h2>
        <div class="customer">{customer_name}</div>
        <p><strong>Contact No.:</strong> &nbsp;{customer_phone}</p>
        <p><strong>State:</strong> &nbsp;24-Gujarat</p>
      </div>
      <div class="invoice-info">
        <h2>Tax Invoice</h2>
        <p><strong>Invoice No.:</strong><span>{invoice_number}</span></p>
        <p><strong>Date:</strong><span>{date}</span></p>
        <p><strong>Place of Supply:</strong><span>24-Gujarat</span></p>
        <p><strong>DISTRIBUTOR:</strong><span>{distributor}</span></p>
      </div>
    </section>
    <table>
      <colgroup><col style="width:4%"><col style="width:24%"><col style="width:10%"><col style="width:8%"><col style="width:11%"><col style="width:11%"><col style="width:11%"><col style="width:11%"><col style="width:10%"></colgroup>
      <thead><tr>
        <th>#</th><th>Item name</th><th>HSN/SAC</th><th class="r">Quantity</th>
        <th class="r">Price/ Unit</th><th class="r">Discount</th>
        <th class="r">Taxable Price/ Unit</th><th class="r">Final Rate</th><th class="r">Amount</th>
      </tr></thead>
      <tbody>
      {rows_html}
      <tr class="total"><td colspan="3">Total</td><td class="r">{total_qty}</td><td></td><td class="r">{money(discount_cents)}</td><td></td><td></td><td class="r">{money(round(float(inv['total']) * 100))}</td></tr>
      </tbody>
    </table>
    <p style="margin-top:12px"><strong>Payment:</strong> {payment}{f" ({payment_ref})" if payment_ref else ""}</p>
    </body></html>"""


@router.get("/invoices/{invoice_id}/print", response_class=HTMLResponse)
def print_invoice(invoice_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    inv = db.execute(
        """SELECT i.*, l.code AS location_code, l.name AS location_name, u.name AS cashier_name
           FROM invoices i
           JOIN locations l ON l.id = i.location_id
           LEFT JOIN users u ON u.id = i.cashier_id
           WHERE i.id = ?""",
        (invoice_id,),
    ).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["status"] != "paid":
        raise HTTPException(409, "Only paid invoices can be printed as receipts")
    if inv["location_id"] not in invoice_location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    if user["role"] == "distributor" and inv["cashier_id"] != user["id"]:
        raise HTTPException(403, "Distributors can only access their own bills")
    items = db.execute(
        """SELECT ii.*, b.title, b.barcode
           FROM invoice_items ii JOIN books b ON b.id = ii.book_id
           WHERE ii.invoice_id = ?""",
        (invoice_id,),
    ).fetchall()
    return invoice_html(inv, items)


@router.get("/invoices/{invoice_id}/pdf")
def download_invoice_pdf(invoice_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    inv = db.execute(
        """SELECT i.*, l.code AS location_code, l.name AS location_name, u.name AS cashier_name
           FROM invoices i
           JOIN locations l ON l.id = i.location_id
           LEFT JOIN users u ON u.id = i.cashier_id
           WHERE i.id = ?""",
        (invoice_id,),
    ).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["status"] != "paid":
        raise HTTPException(409, "Only paid invoices can be downloaded as receipts")
    if inv["location_id"] not in invoice_location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    if user["role"] == "distributor" and inv["cashier_id"] != user["id"]:
        raise HTTPException(403, "Distributors can only access their own bills")
    items = db.execute(
        """SELECT ii.*, b.title, b.barcode
           FROM invoice_items ii JOIN books b ON b.id = ii.book_id
           WHERE ii.invoice_id = ?""",
        (invoice_id,),
    ).fetchall()
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", inv["invoice_number"])
    return Response(
        content=build_invoice_pdf(inv, items),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}.pdf"'},
    )


@router.delete("/invoices/{invoice_id}")
def delete_invoice(invoice_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    if user["role"] == "distributor":
        raise HTTPException(403, "Distributors cannot delete invoices")
    inv = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["location_id"] not in invoice_location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    if user["role"] == "distributor" and inv["cashier_id"] != user["id"]:
        raise HTTPException(403, "Distributors can only access their own bills")
    handed_over = db.execute(
        "SELECT 1 FROM cash_submission_items WHERE invoice_id = ?", (invoice_id,)
    ).fetchone()
    if handed_over:
        raise HTTPException(
            409,
            "This invoice has already been handed over to the temple. Void that payment-in first.",
        )
    if inv["status"] == "pending":
        raise HTTPException(409, "This invoice has a pending Razorpay payment and cannot be deleted")
    stock_restored = inv["status"] == "paid"
    items = db.execute(
        "SELECT * FROM invoice_items WHERE invoice_id = ?",
        (invoice_id,),
    ).fetchall()
    if stock_restored:
        for it in items:
            move_stock(db, it["book_id"], inv["location_id"], it["qty"], "SALE_REVERSED", "invoices", invoice_id, user["id"], "invoice deleted", commit=False)
    db.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    db.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    db.commit()
    log_audit(db, user["id"], "INVOICE_DELETE", "invoices", invoice_id, f"{inv['invoice_number']} restocked {len(items) if stock_restored else 0} lines", user["username"])
    return {
        "ok": True,
        "restocked_lines": len(items) if stock_restored else 0,
        "stock_restored": stock_restored,
    }
