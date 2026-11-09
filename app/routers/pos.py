import re
import secrets
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

PAYMENT_MODES = ("cash", "upi", "card", "razorpay", "ledger")
INVOICE_PAYMENT_METHODS = ("cash", "upi", "card")
INVOICE_SETTLED_STATUSES = ("paid", "partial", "unpaid")
logger = logging.getLogger(__name__)


def invoice_status_for(total, amount_paid) -> str:
    total = round(float(total or 0), 2)
    amount_paid = round(float(amount_paid or 0), 2)
    if total <= 0:
        return "paid"
    if amount_paid <= 0:
        return "unpaid"
    if amount_paid + 0.009 < total:
        return "partial"
    return "paid"


def paid_total(db, invoice_id) -> float:
    row = db.execute(
        "SELECT COALESCE(SUM(amount),0) AS paid FROM invoice_payments WHERE invoice_id = ?",
        (invoice_id,),
    ).fetchone()
    return round(float(row["paid"] or 0), 2)


def invoice_payment_summary(db, invoice_id):
    paid = paid_total(db, invoice_id)
    cash = db.execute(
        """SELECT COALESCE(SUM(amount),0) AS collected
             FROM invoice_payments
            WHERE invoice_id = ? AND method = 'cash'""",
        (invoice_id,),
    ).fetchone()["collected"]
    remitted = db.execute(
        """SELECT COALESCE(SUM(amount),0) AS remitted
             FROM cash_submission_items
            WHERE invoice_id = ?""",
        (invoice_id,),
    ).fetchone()["remitted"]
    collected = round(float(cash or 0), 2)
    remitted = round(float(remitted or 0), 2)
    return {
        "amount_paid": paid,
        "cash_collected": collected,
        "cash_remitted": remitted,
        "cash_remittable": max(0, round(collected - remitted, 2)),
    }


def decorate_invoice(data):
    total = round(float(data.get("total") or 0), 2)
    paid = round(float(data.get("amount_paid") or 0), 2)
    data["amount_paid"] = paid
    data["balance_due"] = max(0, round(total - paid, 2))
    return data



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
    distributor_id: Optional[int] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    payment_mode: str = "cash"
    payment_ref: Optional[str] = None
    amount_received: Optional[float] = None
    discount: float = 0
    items: list[InvoiceItemIn]


class InvoicePaymentIn(BaseModel):
    amount: float
    method: str = "cash"
    reference: Optional[str] = None
    note: Optional[str] = None


class InvoiceEditItemIn(BaseModel):
    barcode: str
    qty: int


class InvoiceEditIn(BaseModel):
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    payment_mode: Optional[str] = None
    payment_ref: Optional[str] = None
    discount: Optional[float] = None
    items: Optional[list[InvoiceEditItemIn]] = None


@router.get("/invoices")
def list_invoices(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    ids = invoice_location_ids(db, user)
    sql = """SELECT i.*, l.code AS location_code, l.name AS location_name, u.name AS cashier_name,
                    COALESCE((SELECT SUM(p.amount) FROM invoice_payments p
                               WHERE p.invoice_id = i.id),0) AS amount_paid
             FROM invoices i
             JOIN locations l ON l.id = i.location_id
             LEFT JOIN users u ON u.id = i.cashier_id
             WHERE i.location_id IN ({})""".format(",".join("?" * len(ids)))
    params = list(ids)
    if user["role"] == "distributor":
        sql += " AND (i.cashier_id = ? OR i.distributor_id = ?)"
        params.extend([user["id"], user["id"]])
    sql += " ORDER BY i.id DESC"
    return [decorate_invoice(dict(r)) for r in db.execute(sql, params).fetchall()]


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
                    COALESCE((SELECT SUM(p.amount) FROM invoice_payments p
                               WHERE p.invoice_id = i.id),0) AS amount_paid,
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
        sql += " AND (i.cashier_id = ? OR i.distributor_id = ?)"
        params.extend([user["id"], user["id"]])
    sql += " ORDER BY i.id DESC, ii.id"
    return [decorate_invoice(dict(row)) for row in db.execute(sql, params).fetchall()]


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
    distributor_id = None
    if data.distributor_id is not None:
        if user["role"] not in ("super_admin", "zone_admin"):
            raise HTTPException(403, "Only a super admin or zone admin can assign a sale to a distributor")
        distributor = db.execute(
            """SELECT id, zone_id FROM users
               WHERE id = ? AND role = 'distributor' AND is_active = 1""",
            (data.distributor_id,),
        ).fetchone()
        if not distributor:
            raise HTTPException(400, "Select an active distributor")
        if user["role"] == "zone_admin" and distributor["zone_id"] != user["zone_id"]:
            raise HTTPException(403, "Zone admins can only bill for distributors in their own zone")
        if loc["type"] != "warehouse" and distributor["zone_id"] != loc_id:
            raise HTTPException(400, "The distributor must belong to the billing zone")
        distributor_id = distributor["id"]

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

    amount_paid = total
    status = "pending"
    if data.payment_mode != "razorpay":
        if data.amount_received is None:
            amount_paid = 0.0 if data.payment_mode == "ledger" else total
        else:
            amount_paid = round(float(data.amount_received), 2)
            if amount_paid < 0:
                raise HTTPException(400, "Amount received cannot be negative")
            if amount_paid > total + 0.009:
                raise HTTPException(400, "Amount received cannot exceed the invoice total")
            amount_paid = min(amount_paid, total)
        status = invoice_status_for(total, amount_paid)

    qr_code = None
    try:
        num = next_invoice_number(db, user)
        stored_payment_mode = "upi" if data.payment_mode == "razorpay" else data.payment_mode
        share_token = secrets.token_urlsafe(24)
        cur = db.execute(
            """INSERT INTO invoices (invoice_number, share_token, location_id, customer_name, customer_phone,
                                     payment_mode, payment_ref, status, subtotal, discount, total, cashier_id,
                                     distributor_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (num, share_token, loc_id, customer_name, customer_phone,
             stored_payment_mode, data.payment_ref, status, subtotal, discount, total, user["id"],
             distributor_id),
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
        elif amount_paid > 0:
            db.execute(
                """INSERT INTO invoice_payments
                       (invoice_id, amount, method, reference, received_by, note)
                   VALUES (?,?,?,?,?,?)""",
                (iid, amount_paid, data.payment_mode, data.payment_ref, user["id"],
                 "Initial payment at billing" if amount_paid < total else None),
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
    log_audit(db, user["id"], "INVOICE_CREATE", "invoices", iid,
              f"{num} total {total} received {amount_paid} {payment_label} {status}", user["username"])
    result = {
        "invoice_id": iid,
        "invoice_number": num,
        "total": total,
        "subtotal": subtotal,
        "discount": discount,
        "amount_paid": amount_paid,
        "balance_due": max(0, round(total - amount_paid, 2)),
        "status": status,
        "payment_mode": payment_label,
        "share_token": share_token,
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
        """SELECT i.*, l.code AS location_code, l.name AS location_name,
                  u.name AS cashier_name, d.name AS distributor_name,
                  COALESCE((SELECT SUM(p.amount) FROM invoice_payments p
                             WHERE p.invoice_id = i.id),0) AS amount_paid
           FROM invoices i
           JOIN locations l ON l.id = i.location_id
           LEFT JOIN users u ON u.id = i.cashier_id
           LEFT JOIN users d ON d.id = i.distributor_id
           WHERE i.id = ?""",
        (invoice_id,),
    ).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["location_id"] not in invoice_location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    if user["role"] == "distributor" and user["id"] not in (inv["cashier_id"], inv["distributor_id"]):
        raise HTTPException(403, "Distributors can only access their own bills")
    items = db.execute(
        """SELECT ii.*, b.title, b.barcode
           FROM invoice_items ii JOIN books b ON b.id = ii.book_id
           WHERE ii.invoice_id = ?""",
        (invoice_id,),
    ).fetchall()
    payments = db.execute(
        """SELECT p.*, u.name AS received_by_name
             FROM invoice_payments p
             LEFT JOIN users u ON u.id = p.received_by
            WHERE p.invoice_id = ?
            ORDER BY p.id""",
        (invoice_id,),
    ).fetchall()
    data = decorate_invoice(dict(inv))
    data["items"] = [dict(r) for r in items]
    data["payments"] = [dict(r) for r in payments]
    data.update(invoice_payment_summary(db, invoice_id))
    data["handed_over"] = bool(
        db.execute(
            "SELECT 1 FROM cash_submission_items WHERE invoice_id = ? LIMIT 1",
            (invoice_id,),
        ).fetchone()
    )
    return data


@router.patch("/invoices/{invoice_id}")
def update_invoice(invoice_id: int, data: InvoiceEditIn, db=Depends(get_db), user=Depends(require_roles("super_admin", "distributor"))):
    inv = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["status"] in ("pending", "failed"):
        raise HTTPException(409, "Pending or failed invoices cannot be edited")
    if inv["location_id"] not in invoice_location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    if user["role"] == "distributor" and user["id"] not in (inv["cashier_id"], inv["distributor_id"]):
        raise HTTPException(403, "Distributors can only edit their own bills")

    handed_over = db.execute(
        "SELECT 1 FROM cash_submission_items WHERE invoice_id = ? LIMIT 1", (invoice_id,)
    ).fetchone()
    recorded_paid = paid_total(db, invoice_id)
    partially_paid = 0 < recorded_paid < round(float(inv["total"] or 0), 2)
    razorpay_invoice = inv["razorpay_qr_code_id"] is not None
    amount_change = data.items is not None or data.discount is not None or data.payment_mode is not None
    if handed_over and amount_change:
        raise HTTPException(
            409,
            "This invoice has already been handed over to the temple. Void that payment-in before editing its items or amount.",
        )
    if partially_paid and amount_change:
        raise HTTPException(
            409,
            "Payments have already been recorded on this invoice. Void those payments before editing its items or amount.",
        )
    if razorpay_invoice and amount_change:
        raise HTTPException(
            409,
            "Razorpay invoices can only have their customer and payment reference details edited.",
        )

    old_items = db.execute(
        "SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY id", (invoice_id,)
    ).fetchall()
    old_by_book = {row["book_id"]: row for row in old_items}
    subtotal = round(sum(float(row["line_total"]) for row in old_items), 2)

    new_lines = None
    if data.items is not None:
        if not data.items:
            raise HTTPException(400, "An invoice must have at least one item")
        resolved = {}
        for item in data.items:
            if item.qty <= 0:
                raise HTTPException(400, "Quantities must be positive")
            book = db.execute(
                "SELECT * FROM books WHERE barcode = ? OR isbn = ?",
                (item.barcode, item.barcode),
            ).fetchone()
            if not book:
                raise HTTPException(400, f"Unknown barcode: {item.barcode}")
            if book["id"] in resolved:
                resolved[book["id"]] = (book, resolved[book["id"]][1] + item.qty)
            else:
                resolved[book["id"]] = (book, item.qty)

        deltas = []
        new_lines = []
        for book_id, (book, qty) in resolved.items():
            old = old_by_book.get(book_id)
            unit_price = float(old["unit_price"]) if old else float(book["price"])
            if old:
                difference = qty - int(old["qty"])
                if difference > 0 and not check_available(db, book_id, inv["location_id"], difference):
                    raise HTTPException(400, f"Insufficient stock for '{book['title']}' at this location")
                if difference:
                    deltas.append((book_id, -difference))
            else:
                if not check_available(db, book_id, inv["location_id"], qty):
                    raise HTTPException(400, f"Insufficient stock for '{book['title']}' at this location")
                deltas.append((book_id, -qty))
            new_lines.append((book_id, qty, unit_price, round(unit_price * qty, 2)))
        for book_id, old in old_by_book.items():
            if book_id not in resolved:
                deltas.append((book_id, int(old["qty"])))
        subtotal = round(sum(line[3] for line in new_lines), 2)

        try:
            for book_id, delta in deltas:
                if delta:
                    move_stock(
                        db, book_id, inv["location_id"], delta, "SALE_ADJUSTED",
                        "invoices", invoice_id, user["id"], "invoice edited", commit=False,
                    )
        except ValueError as error:
            db.rollback()
            raise HTTPException(400, str(error)) from error

    customer_name = inv["customer_name"]
    if data.customer_name is not None:
        customer_name = data.customer_name.strip()
        if not customer_name:
            raise HTTPException(400, "Customer name is required")
    customer_phone = inv["customer_phone"]
    if data.customer_phone is not None:
        customer_phone = data.customer_phone.strip()
        if not customer_phone:
            raise HTTPException(400, "Phone number is required")
    payment_ref = inv["payment_ref"]
    if data.payment_ref is not None:
        payment_ref = data.payment_ref.strip() or None

    payment_mode = inv["payment_mode"]
    if data.payment_mode is not None:
        if data.payment_mode not in ("cash", "upi", "card", "ledger"):
            raise HTTPException(400, "Invalid payment mode")
        payment_mode = data.payment_mode

    if data.discount is not None:
        if data.discount < 0:
            raise HTTPException(400, "Discount cannot be negative")
        discount = min(float(data.discount), subtotal)
    else:
        discount = min(float(inv["discount"] or 0), subtotal)
    total = round(subtotal - discount, 2)
    status = inv["status"]
    fully_paid_before = recorded_paid > 0 and not partially_paid
    if not razorpay_invoice:
        # A bill that was already settled follows its corrected total, so the
        # single payment is adjusted rather than leaving a phantom balance.
        status = "paid" if fully_paid_before else invoice_status_for(total, recorded_paid)

    db.execute(
        """UPDATE invoices
              SET customer_name = ?, customer_phone = ?, payment_mode = ?,
                  payment_ref = ?, subtotal = ?, discount = ?, total = ?, status = ?
            WHERE id = ?""",
        (customer_name, customer_phone, payment_mode, payment_ref, subtotal, discount, total,
         status, invoice_id),
    )
    if fully_paid_before and not razorpay_invoice:
        db.execute("DELETE FROM invoice_payments WHERE invoice_id = ?", (invoice_id,))
        if total > 0:
            db.execute(
                """INSERT INTO invoice_payments
                       (invoice_id, amount, method, reference, received_by, note)
                   VALUES (?,?,?,?,?,?)""",
                (invoice_id, total, payment_mode, payment_ref, user["id"],
                 "Adjusted after invoice edit"),
            )
    if new_lines is not None:
        db.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
        for book_id, qty, unit_price, line_total in new_lines:
            db.execute(
                "INSERT INTO invoice_items (invoice_id, book_id, qty, unit_price, line_total) VALUES (?,?,?,?,?)",
                (invoice_id, book_id, qty, unit_price, line_total),
            )
    db.commit()
    log_audit(
        db, user["id"], "INVOICE_UPDATE", "invoices", invoice_id,
        f"{inv['invoice_number']} updated to total {total}", user["username"],
    )

    updated = db.execute(
        """SELECT i.*, l.code AS location_code, l.name AS location_name,
                  u.name AS cashier_name, d.name AS distributor_name,
                  COALESCE((SELECT SUM(p.amount) FROM invoice_payments p
                             WHERE p.invoice_id = i.id),0) AS amount_paid
           FROM invoices i
           JOIN locations l ON l.id = i.location_id
           LEFT JOIN users u ON u.id = i.cashier_id
           LEFT JOIN users d ON d.id = i.distributor_id
           WHERE i.id = ?""",
        (invoice_id,),
    ).fetchone()
    items = db.execute(
        """SELECT ii.*, b.title, b.barcode
           FROM invoice_items ii JOIN books b ON b.id = ii.book_id
           WHERE ii.invoice_id = ?""",
        (invoice_id,),
    ).fetchall()
    result = decorate_invoice(dict(updated))
    result["items"] = [dict(row) for row in items]
    return result


@router.post("/invoices/{invoice_id}/payments")
def receive_invoice_payment(
    invoice_id: int,
    data: InvoicePaymentIn,
    db=Depends(get_db),
    user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple")),
):
    """Record money collected later against a partial or credit invoice."""
    inv = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["location_id"] not in invoice_location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    if user["role"] == "distributor" and user["id"] not in (inv["cashier_id"], inv["distributor_id"]):
        raise HTTPException(403, "Distributors can only collect payments for their own bills")
    if inv["razorpay_qr_code_id"] or inv["status"] in ("pending", "failed"):
        raise HTTPException(409, "Razorpay invoices are settled only by Razorpay and cannot take a manual payment")
    if data.method not in INVOICE_PAYMENT_METHODS:
        raise HTTPException(400, f"Invalid payment method. Valid: {', '.join(INVOICE_PAYMENT_METHODS)}")

    total = round(float(inv["total"] or 0), 2)
    already_paid = paid_total(db, invoice_id)
    balance = max(0, round(total - already_paid, 2))
    if balance <= 0:
        raise HTTPException(409, "This invoice is already fully paid")
    amount = round(float(data.amount), 2)
    if amount <= 0:
        raise HTTPException(400, "Payment amount must be positive")
    if amount > balance + 0.009:
        raise HTTPException(400, f"Payment exceeds the outstanding balance of {balance:.2f}")

    try:
        cur = db.execute(
            """INSERT INTO invoice_payments
                   (invoice_id, amount, method, reference, received_by, note)
               VALUES (?,?,?,?,?,?)""",
            (invoice_id, amount, data.method, (data.reference or "").strip() or None,
             user["id"], (data.note or "").strip() or None),
        )
        payment_id = cur.lastrowid
        new_paid = round(already_paid + amount, 2)
        status = invoice_status_for(total, new_paid)
        db.execute("UPDATE invoices SET status = ? WHERE id = ?", (status, invoice_id))
        db.commit()
    except sqlite3.Error:
        db.rollback()
        raise

    log_audit(
        db, user["id"], "INVOICE_PAYMENT", "invoices", invoice_id,
        f"{inv['invoice_number']} received {amount:.2f} {data.method}; balance "
        f"{max(0, round(total - new_paid, 2)):.2f}",
        user["username"],
    )
    return {
        "ok": True,
        "payment_id": payment_id,
        "invoice_id": invoice_id,
        "invoice_number": inv["invoice_number"],
        "amount": amount,
        "amount_paid": new_paid,
        "balance_due": max(0, round(total - new_paid, 2)),
        "status": status,
    }


def invoice_date_ist(value):
    return format_ist_date(value)


def _value(row, key, default=0):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def invoice_html(inv, items, pdf_url=None):
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
    distributor = escape(str(inv["distributor_name"] or inv["cashier_name"] or "-"))
    invoice_number = escape(str(inv["invoice_number"]))
    date = invoice_date_ist(inv["created_at"])
    payment_ref = escape(str(inv["payment_ref"] or ""))
    payment_label = "razorpay" if inv["razorpay_qr_code_id"] else inv["payment_mode"]
    payment = escape(str(payment_label or "").upper())
    total_cents = round(float(inv["total"]) * 100)
    paid_cents = round(float(_value(inv, "amount_paid", float(inv["total"]))) * 100)
    balance_cents = max(0, total_cents - paid_cents)
    balance_row = (
        f"<p><strong>Amount paid:</strong> {money(paid_cents)} &nbsp; "
        f"<strong>Balance due:</strong> {money(balance_cents)}</p>"
        if balance_cents > 0 else
        f"<p><strong>Amount paid:</strong> {money(paid_cents)}</p>"
    )
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
      .share-bar {{ display: flex; gap: 10px; justify-content: flex-end; padding: 12px 4px 2px; }}
      .share-bar a, .share-bar button {{ border: 0; border-radius: 6px; background: #e71936; color: #fff; cursor: pointer; font: 600 13px Arial, sans-serif; padding: 10px 16px; text-decoration: none; }}
      @page {{ size: A4 landscape; margin: 8mm; }}
      @media print {{ .share-bar {{ display: none; }} body {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }} }}
    </style></head><body>
    {f'<div class="share-bar"><button onclick="window.print()">Print</button><a href="{pdf_url}">Download PDF</a></div>' if pdf_url else ''}
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
    <div class="payment-summary">
      {balance_row}
    </div>
    </body></html>"""


@router.get("/invoices/{invoice_id}/print", response_class=HTMLResponse)
def print_invoice(invoice_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    inv = db.execute(
        """SELECT i.*, l.code AS location_code, l.name AS location_name,
                  u.name AS cashier_name, d.name AS distributor_name,
                  COALESCE((SELECT SUM(p.amount) FROM invoice_payments p
                             WHERE p.invoice_id = i.id),0) AS amount_paid
           FROM invoices i
           JOIN locations l ON l.id = i.location_id
           LEFT JOIN users u ON u.id = i.cashier_id
           LEFT JOIN users d ON d.id = i.distributor_id
           WHERE i.id = ?""",
        (invoice_id,),
    ).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["status"] not in INVOICE_SETTLED_STATUSES:
        raise HTTPException(409, "Only billed invoices can be printed as receipts")
    if inv["location_id"] not in invoice_location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    if user["role"] == "distributor" and user["id"] not in (inv["cashier_id"], inv["distributor_id"]):
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
        """SELECT i.*, l.code AS location_code, l.name AS location_name,
                  u.name AS cashier_name, d.name AS distributor_name,
                  COALESCE((SELECT SUM(p.amount) FROM invoice_payments p
                             WHERE p.invoice_id = i.id),0) AS amount_paid
           FROM invoices i
           JOIN locations l ON l.id = i.location_id
           LEFT JOIN users u ON u.id = i.cashier_id
           LEFT JOIN users d ON d.id = i.distributor_id
           WHERE i.id = ?""",
        (invoice_id,),
    ).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["status"] not in INVOICE_SETTLED_STATUSES:
        raise HTTPException(409, "Only billed invoices can be downloaded as receipts")
    if inv["location_id"] not in invoice_location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    if user["role"] == "distributor" and user["id"] not in (inv["cashier_id"], inv["distributor_id"]):
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
    if user["role"] == "distributor" and user["id"] not in (inv["cashier_id"], inv["distributor_id"]):
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
    stock_restored = inv["status"] in INVOICE_SETTLED_STATUSES
    items = db.execute(
        "SELECT * FROM invoice_items WHERE invoice_id = ?",
        (invoice_id,),
    ).fetchall()
    if stock_restored:
        for it in items:
            move_stock(db, it["book_id"], inv["location_id"], it["qty"], "SALE_REVERSED", "invoices", invoice_id, user["id"], "invoice deleted", commit=False)
    db.execute("DELETE FROM invoice_payments WHERE invoice_id = ?", (invoice_id,))
    db.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    db.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    db.commit()
    log_audit(db, user["id"], "INVOICE_DELETE", "invoices", invoice_id, f"{inv['invoice_number']} restocked {len(items) if stock_restored else 0} lines", user["username"])
    return {
        "ok": True,
        "restocked_lines": len(items) if stock_restored else 0,
        "stock_restored": stock_restored,
    }
