from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..auth import require_roles
from ..database import get_db
from ..services.audit import log_audit
from ..services.ledger import check_available, move_stock
from ..services.scope import location_ids

router = APIRouter(prefix="/api/pos", tags=["pos"])

PAYMENT_MODES = ("cash", "upi", "card", "ledger")


class InvoiceItemIn(BaseModel):
    barcode: str
    qty: int = 1


class InvoiceIn(BaseModel):
    location_id: int = None
    customer_name: str = None
    customer_phone: str = None
    customer_email: str = None
    payment_mode: str = "cash"
    payment_ref: str = None
    discount: float = 0
    items: list[InvoiceItemIn]


@router.get("/invoices")
def list_invoices(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    ids = location_ids(db, user)
    sql = """SELECT i.*, l.code AS location_code, l.name AS location_name, u.username AS cashier_name
             FROM invoices i
             JOIN locations l ON l.id = i.location_id
             LEFT JOIN users u ON u.id = i.cashier_id
             WHERE i.location_id IN ({})
             ORDER BY i.id DESC""".format(",".join("?" * len(ids)))
    return [dict(r) for r in db.execute(sql, list(ids)).fetchall()]


@router.post("/invoices")
def create_invoice(data: InvoiceIn, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    if not data.items:
        raise HTTPException(400, "Cart is empty")
    if data.payment_mode not in PAYMENT_MODES:
        raise HTTPException(400, f"Invalid payment mode. Valid: {', '.join(PAYMENT_MODES)}")
    loc_id = data.location_id or user["location_id"] or user["zone_id"]
    if not loc_id or loc_id not in location_ids(db, user):
        raise HTTPException(400, "You have no billing location")
    loc = db.execute("SELECT * FROM locations WHERE id = ?", (loc_id,)).fetchone()

    resolved = []
    for it in data.items:
        if it.qty <= 0:
            raise HTTPException(400, "Quantities must be positive")
        book = db.execute("SELECT * FROM books WHERE barcode = ? OR isbn = ?", (it.barcode, it.barcode)).fetchone()
        if not book:
            raise HTTPException(400, f"Unknown barcode: {it.barcode}")
        if not check_available(db, book["id"], loc_id, it.qty):
            raise HTTPException(400, f"Insufficient stock for '{book['title']}' at {loc['name']}")
        resolved.append((book, it.qty))

    subtotal = sum(b["price"] * q for b, q in resolved)
    discount = min(data.discount, subtotal)
    total = round(subtotal - discount, 2)

    num = f"INV-{loc['code']}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    cur = db.execute(
        """INSERT INTO invoices (invoice_number, location_id, customer_name, customer_phone, customer_email,
                                 payment_mode, payment_ref, status, subtotal, discount, total, cashier_id)
           VALUES (?,?,?,?,?,?,?, 'paid', ?, ?, ?, ?)""",
        (num, loc_id, data.customer_name, data.customer_phone, data.customer_email,
         data.payment_mode, data.payment_ref, subtotal, discount, total, user["id"]),
    )
    iid = cur.lastrowid
    for book, q in resolved:
        db.execute(
            "INSERT INTO invoice_items (invoice_id, book_id, qty, unit_price, line_total) VALUES (?,?,?,?,?)",
            (iid, book["id"], q, book["price"], round(book["price"] * q, 2)),
        )
        move_stock(db, book["id"], loc_id, -q, "SALE_OUT", "invoices", iid, user["id"], num)
    db.commit()
    log_audit(db, user["id"], "INVOICE_CREATE", "invoices", iid, f"{num} total {total} {data.payment_mode}", user["username"])
    return {"invoice_id": iid, "invoice_number": num, "total": total, "subtotal": subtotal, "discount": discount}


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    inv = db.execute(
        """SELECT i.*, l.code AS location_code, l.name AS location_name, u.username AS cashier_name
           FROM invoices i
           JOIN locations l ON l.id = i.location_id
           LEFT JOIN users u ON u.id = i.cashier_id
           WHERE i.id = ?""",
        (invoice_id,),
    ).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["location_id"] not in location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    items = db.execute(
        """SELECT ii.*, b.title, b.barcode
           FROM invoice_items ii JOIN books b ON b.id = ii.book_id
           WHERE ii.invoice_id = ?""",
        (invoice_id,),
    ).fetchall()
    data = dict(inv)
    data["items"] = [dict(r) for r in items]
    return data


def invoice_html(inv, items, org_name="ISKCON"):
    rows = "".join(
        f"<tr><td>{it['title']}</td><td>{it['qty']}</td><td class='r'>{it['unit_price']:.2f}</td><td class='r'>{it['line_total']:.2f}</td></tr>"
        for it in items
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Invoice {inv['invoice_number']}</title>
    <style>
      body {{ font-family: Arial, sans-serif; margin: 32px; color: #222; }}
      .head {{ border-bottom: 2px solid #8a5a1f; padding-bottom: 12px; margin-bottom: 20px; }}
      h1 {{ margin: 0; font-size: 22px; color: #8a5a1f; }}
      .sub {{ color: #666; font-size: 13px; }}
      table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
      th, td {{ border: 1px solid #ddd; padding: 8px; font-size: 13px; text-align: left; }}
      th {{ background: #f5efe2; }}
      .r {{ text-align: right; }}
      .total {{ font-size: 15px; font-weight: bold; }}
      .foot {{ margin-top: 24px; font-size: 12px; color: #666; }}
      @media print {{ body {{ margin: 8mm; }} }}
    </style></head><body>
    <div class="head">
      <h1>{org_name} Book Distribution</h1>
      <div class="sub">{inv['location_name']} &mdash; {inv['invoice_number']}</div>
      <div class="sub">Date: {inv['created_at']} &nbsp;|&nbsp; Cashier: {inv['cashier_name'] or '-'}</div>
    </div>
    <div class="sub">Customer: {inv['customer_name'] or '-'} {inv['customer_phone'] or ''} {inv['customer_email'] or ''}</div>
    <table>
      <tr><th>Title</th><th>Qty</th><th class="r">Unit Price</th><th class="r">Amount</th></tr>
      {rows}
      <tr><td colspan="3" class="r">Subtotal</td><td class="r">{inv['subtotal']:.2f}</td></tr>
      <tr><td colspan="3" class="r">Discount</td><td class="r">{inv['discount']:.2f}</td></tr>
      <tr class="total"><td colspan="3" class="r">Total</td><td class="r">{inv['total']:.2f}</td></tr>
    </table>
    <div class="foot">Payment: {inv['payment_mode'].upper()}{' (' + inv['payment_ref'] + ')' if inv['payment_ref'] else ''} &nbsp;|&nbsp; Status: {inv['status'].upper()}</div>
    <div class="foot">Thank you. &copy; {org_name}</div>
    </body></html>"""


@router.get("/invoices/{invoice_id}/print", response_class=HTMLResponse)
def print_invoice(invoice_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    inv = db.execute(
        """SELECT i.*, l.code AS location_code, l.name AS location_name, u.username AS cashier_name
           FROM invoices i
           JOIN locations l ON l.id = i.location_id
           LEFT JOIN users u ON u.id = i.cashier_id
           WHERE i.id = ?""",
        (invoice_id,),
    ).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["location_id"] not in location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    items = db.execute(
        """SELECT ii.*, b.title, b.barcode
           FROM invoice_items ii JOIN books b ON b.id = ii.book_id
           WHERE ii.invoice_id = ?""",
        (invoice_id,),
    ).fetchall()
    return invoice_html(inv, items)


@router.delete("/invoices/{invoice_id}")
def delete_invoice(invoice_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    inv = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["location_id"] not in location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    items = db.execute(
        "SELECT * FROM invoice_items WHERE invoice_id = ?",
        (invoice_id,),
    ).fetchall()
    for it in items:
        move_stock(db, it["book_id"], inv["location_id"], it["qty"], "SALE_REVERSED", "invoices", invoice_id, user["id"], "invoice deleted")
    db.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    db.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    db.commit()
    log_audit(db, user["id"], "INVOICE_DELETE", "invoices", invoice_id, f"{inv['invoice_number']} restocked {len(items)} lines", user["username"])
    return {"ok": True, "restocked_lines": len(items)}
