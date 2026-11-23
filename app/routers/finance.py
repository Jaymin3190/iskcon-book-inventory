"""Purchases (book inward bills) and operating expenses.

These are the two missing halves of a billing system: the app could already
sell books, but it did not know what the books cost or what it spent to run the
temple. Purchases add stock and set the landed cost that Profit & Loss uses;
expenses record the day-to-day running costs.
"""

import sqlite3
from datetime import date as date_cls
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_roles
from ..database import get_db
from ..services.audit import log_audit
from ..services.ledger import check_available, move_stock
from ..services.scope import finance_location_ids

router = APIRouter(prefix="/api/finance", tags=["finance"])

PURCHASE_PAYMENT_MODES = ("cash", "upi", "card", "credit")
EXPENSE_CATEGORIES = (
    "Rent",
    "Salary",
    "Transport",
    "Printing",
    "Programme",
    "Utilities",
    "Office",
    "Marketing",
    "Other",
)


def _as_date(value, field="date"):
    try:
        return date_cls.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as error:
        raise HTTPException(400, f"{field} must be a date like 2026-11-20") from error


def _money(value):
    return round(float(value or 0), 2)


def _location_name(db, location_id):
    if location_id is None:
        return None
    row = db.execute("SELECT name FROM locations WHERE id = ?", (location_id,)).fetchone()
    return row["name"] if row else None


def _recalculate_book_cost(db, book_id):
    """Moving average of everything ever purchased for this book.

    Freight and purchase discounts are already folded into
    ``landed_unit_cost``, so the average reflects what a copy really cost.
    """
    row = db.execute(
        """SELECT COALESCE(SUM(qty * landed_unit_cost), 0) AS value,
                  COALESCE(SUM(qty), 0) AS units
             FROM purchase_items WHERE book_id = ?""",
        (book_id,),
    ).fetchone()
    if row["units"]:
        db.execute(
            "UPDATE books SET cost_price = ? WHERE id = ?",
            (round(float(row["value"]) / float(row["units"]), 2), book_id),
        )


class PurchaseItemIn(BaseModel):
    book_id: int
    qty: int
    unit_cost: float = 0


class PurchaseIn(BaseModel):
    supplier: str
    bill_number: Optional[str] = None
    location_id: int
    purchase_date: str
    freight: float = 0
    discount: float = 0
    amount_paid: float = 0
    payment_mode: str = "credit"
    note: Optional[str] = None
    client_request_id: Optional[str] = None
    items: list[PurchaseItemIn]


def _purchase_result(db, purchase, duplicate=False):
    """The response used for a new purchase and for an idempotent retry."""
    totals = db.execute(
        """SELECT COUNT(*) AS items, COALESCE(SUM(qty),0) AS units
             FROM purchase_items WHERE purchase_id = ?""",
        (purchase["id"],),
    ).fetchone()
    return {
        "purchase_id": purchase["id"],
        "total": _money(purchase["total"]),
        "subtotal": _money(purchase["subtotal"]),
        "items": int(totals["items"] or 0),
        "units": int(totals["units"] or 0),
        "duplicate": duplicate,
    }


@router.get("/purchases")
def list_purchases(
    frm: str = None,
    to: str = None,
    location_id: int = None,
    db=Depends(get_db),
    user=Depends(require_roles("super_admin", "zone_admin", "temple")),
):
    ids = finance_location_ids(db, user)
    sql = """SELECT p.*, l.name AS location_name, u.username AS created_by_name,
                    (SELECT COUNT(*) FROM purchase_items pi WHERE pi.purchase_id = p.id) AS item_count,
                    (SELECT COALESCE(SUM(pi.qty),0) FROM purchase_items pi WHERE pi.purchase_id = p.id) AS units
               FROM purchases p
               JOIN locations l ON l.id = p.location_id
               LEFT JOIN users u ON u.id = p.created_by
              WHERE p.location_id IN ({})""".format(",".join("?" * len(ids)))
    params = list(ids)
    if frm:
        sql += " AND p.purchase_date >= ?"
        params.append(frm)
    if to:
        sql += " AND p.purchase_date <= ?"
        params.append(to)
    if location_id is not None:
        if location_id not in ids:
            return []
        sql += " AND p.location_id = ?"
        params.append(location_id)
    sql += " ORDER BY p.purchase_date DESC, p.id DESC"
    rows = []
    for row in db.execute(sql, params).fetchall():
        data = dict(row)
        data["balance"] = max(0, _money(data["total"] - data["amount_paid"]))
        rows.append(data)
    return rows


@router.get("/purchases/{purchase_id}")
def get_purchase(
    purchase_id: int,
    db=Depends(get_db),
    user=Depends(require_roles("super_admin", "zone_admin", "temple")),
):
    purchase = db.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
    if not purchase:
        raise HTTPException(404, "Purchase not found")
    if purchase["location_id"] not in finance_location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    items = db.execute(
        """SELECT pi.*, b.title, b.barcode
             FROM purchase_items pi JOIN books b ON b.id = pi.book_id
            WHERE pi.purchase_id = ? ORDER BY pi.id""",
        (purchase_id,),
    ).fetchall()
    data = dict(purchase)
    data["location_name"] = _location_name(db, purchase["location_id"])
    data["items"] = [dict(r) for r in items]
    data["balance"] = max(0, _money(data["total"] - data["amount_paid"]))
    return data


@router.post("/purchases")
def create_purchase(
    data: PurchaseIn,
    db=Depends(get_db),
    user=Depends(require_roles("super_admin", "temple")),
):
    request_id = (data.client_request_id or "").strip() or None
    if request_id and len(request_id) > 120:
        raise HTTPException(400, "Invalid client request id")
    if request_id:
        existing = db.execute(
            "SELECT * FROM purchases WHERE client_request_id = ?", (request_id,)
        ).fetchone()
        if existing:
            if existing["created_by"] != user["id"]:
                raise HTTPException(409, "This request id is already in use")
            return _purchase_result(db, existing, duplicate=True)
    if not data.items:
        raise HTTPException(400, "A purchase bill needs at least one book")
    if data.location_id not in finance_location_ids(db, user):
        raise HTTPException(403, "You cannot record a purchase at this location")
    supplier = (data.supplier or "").strip()
    if not supplier:
        raise HTTPException(400, "Supplier is required")
    purchase_date = _as_date(data.purchase_date, "Purchase date")
    bill_number = (data.bill_number or "").strip() or None
    if bill_number:
        duplicate_bill = db.execute(
            "SELECT id FROM purchases WHERE supplier = ? AND bill_number = ?",
            (supplier, bill_number),
        ).fetchone()
        if duplicate_bill:
            raise HTTPException(
                409,
                f"Supplier bill {bill_number} is already recorded as purchase #{duplicate_bill['id']}",
            )
    if data.payment_mode not in PURCHASE_PAYMENT_MODES:
        raise HTTPException(400, "Invalid payment mode")
    if data.freight < 0 or data.discount < 0:
        raise HTTPException(400, "Freight and discount cannot be negative")

    resolved = {}
    for item in data.items:
        if item.qty <= 0:
            raise HTTPException(400, "Quantities must be positive")
        if item.unit_cost < 0:
            raise HTTPException(400, "Unit cost cannot be negative")
        book = db.execute("SELECT * FROM books WHERE id = ?", (item.book_id,)).fetchone()
        if not book:
            raise HTTPException(400, f"Unknown book: {item.book_id}")
        if item.book_id in resolved:
            existing, qty = resolved[item.book_id]
            if abs(existing - item.unit_cost) > 0.009:
                raise HTTPException(
                    400,
                    f"'{book['title']}' appears twice with different costs; merge it into one line",
                )
            resolved[item.book_id] = (item.unit_cost, qty + item.qty)
        else:
            resolved[item.book_id] = (item.unit_cost, item.qty)

    lines = [
        (book_id, qty, unit_cost, round(qty * unit_cost, 2))
        for book_id, (unit_cost, qty) in resolved.items()
    ]
    subtotal = round(sum(line[3] for line in lines), 2)
    if data.discount > subtotal + 0.009:
        raise HTTPException(400, "Discount cannot exceed the bill subtotal")
    total = round(subtotal - data.discount + data.freight, 2)
    amount_paid = _money(data.amount_paid)
    if amount_paid < 0:
        raise HTTPException(400, "Amount paid cannot be negative")
    if amount_paid > total + 0.009:
        raise HTTPException(400, "Amount paid cannot exceed the purchase total")

    try:
        cur = db.execute(
            """INSERT INTO purchases (bill_number, supplier, location_id, purchase_date,
                                      subtotal, freight, discount, total, amount_paid,
                                      payment_mode, note, created_by, client_request_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                bill_number,
                supplier,
                data.location_id,
                purchase_date,
                subtotal,
                _money(data.freight),
                _money(data.discount),
                total,
                amount_paid,
                data.payment_mode,
                (data.note or "").strip() or None,
                user["id"],
                request_id,
            ),
        )
        purchase_id = cur.lastrowid
        for book_id, qty, unit_cost, line_total in lines:
            share = line_total / subtotal if subtotal else 1 / len(lines)
            landed = (line_total - data.discount * share + data.freight * share) / qty
            db.execute(
                """INSERT INTO purchase_items
                       (purchase_id, book_id, qty, unit_cost, landed_unit_cost, line_total)
                   VALUES (?,?,?,?,?,?)""",
                (purchase_id, book_id, qty, unit_cost, round(landed, 2), line_total),
            )
            move_stock(
                db, book_id, data.location_id, qty, "PURCHASE_IN", "purchases",
                purchase_id, user["id"], bill_number or supplier, commit=False,
            )
        for book_id in resolved:
            _recalculate_book_cost(db, book_id)
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        if request_id:
            existing = db.execute(
                "SELECT * FROM purchases WHERE client_request_id = ?", (request_id,)
            ).fetchone()
            if existing and existing["created_by"] == user["id"]:
                return _purchase_result(db, existing, duplicate=True)
        raise
    except sqlite3.Error:
        db.rollback()
        raise

    log_audit(
        db, user["id"], "PURCHASE_CREATE", "purchases", purchase_id,
        f"{supplier} bill {bill_number or '-'} total {total} at location {data.location_id}",
        user["username"],
    )
    saved = db.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
    return _purchase_result(db, saved)


@router.delete("/purchases/{purchase_id}")
def delete_purchase(
    purchase_id: int,
    db=Depends(get_db),
    user=Depends(require_roles("super_admin", "temple")),
):
    purchase = db.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
    if not purchase:
        raise HTTPException(404, "Purchase not found")
    if purchase["location_id"] not in finance_location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    items = db.execute(
        "SELECT * FROM purchase_items WHERE purchase_id = ?", (purchase_id,)
    ).fetchall()
    for item in items:
        if not check_available(db, item["book_id"], purchase["location_id"], item["qty"]):
            raise HTTPException(
                409,
                "Cannot delete: some of this stock has already been sold or transferred",
            )
    for item in items:
        move_stock(
            db, item["book_id"], purchase["location_id"], -int(item["qty"]),
            "PURCHASE_DELETE", "purchases", purchase_id, user["id"],
            "purchase bill deleted", commit=False,
        )
    db.execute("DELETE FROM purchase_items WHERE purchase_id = ?", (purchase_id,))
    db.execute("DELETE FROM purchases WHERE id = ?", (purchase_id,))
    for item in items:
        _recalculate_book_cost(db, item["book_id"])
    db.commit()
    log_audit(
        db, user["id"], "PURCHASE_DELETE", "purchases", purchase_id,
        f"{purchase['supplier']} bill {purchase['bill_number'] or '-'} reversed",
        user["username"],
    )
    return {"ok": True}


class ExpenseIn(BaseModel):
    expense_date: str
    category: str
    amount: float
    location_id: Optional[int] = None
    vendor: Optional[str] = None
    note: Optional[str] = None
    client_request_id: Optional[str] = None


def _expense_scope_sql(db, user):
    ids = finance_location_ids(db, user)
    placeholders = ",".join("?" * len(ids))
    if user["role"] in ("super_admin", "temple"):
        return f"(location_id IN ({placeholders}) OR location_id IS NULL)", list(ids)
    return f"location_id IN ({placeholders})", list(ids)


@router.get("/expenses")
def list_expenses(
    frm: str = None,
    to: str = None,
    location_id: int = None,
    db=Depends(get_db),
    user=Depends(require_roles("super_admin", "zone_admin", "temple")),
):
    where, params = _expense_scope_sql(db, user)
    sql = f"""SELECT e.*, l.name AS location_name, u.username AS created_by_name
                FROM expenses e
                LEFT JOIN locations l ON l.id = e.location_id
                LEFT JOIN users u ON u.id = e.created_by
               WHERE {where}"""
    if frm:
        sql += " AND e.expense_date >= ?"
        params.append(frm)
    if to:
        sql += " AND e.expense_date <= ?"
        params.append(to)
    if location_id is not None:
        sql += " AND e.location_id = ?"
        params.append(location_id)
    sql += " ORDER BY e.expense_date DESC, e.id DESC"
    return [dict(r) for r in db.execute(sql, params).fetchall()]


@router.post("/expenses")
def create_expense(
    data: ExpenseIn,
    db=Depends(get_db),
    user=Depends(require_roles("super_admin", "zone_admin", "temple")),
):
    request_id = (data.client_request_id or "").strip() or None
    if request_id and len(request_id) > 120:
        raise HTTPException(400, "Invalid client request id")
    if request_id:
        existing = db.execute(
            "SELECT * FROM expenses WHERE client_request_id = ?", (request_id,)
        ).fetchone()
        if existing:
            if existing["created_by"] != user["id"]:
                raise HTTPException(409, "This request id is already in use")
            return {"id": existing["id"], "duplicate": True}
    expense_date = _as_date(data.expense_date, "Expense date")
    category = (data.category or "").strip()
    if not category:
        raise HTTPException(400, "Category is required")
    if data.amount <= 0:
        raise HTTPException(400, "Expense amount must be more than zero")
    location_id = data.location_id
    if location_id is not None:
        if location_id not in finance_location_ids(db, user):
            raise HTTPException(403, "You cannot record an expense at this location")
    elif user["role"] == "zone_admin":
        raise HTTPException(400, "Choose a location for this expense")
    try:
        cur = db.execute(
            """INSERT INTO expenses (expense_date, category, amount, location_id, vendor, note,
                                     created_by, client_request_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                expense_date,
                category,
                _money(data.amount),
                location_id,
                (data.vendor or "").strip() or None,
                (data.note or "").strip() or None,
                user["id"],
                request_id,
            ),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        if request_id:
            existing = db.execute(
                "SELECT id, created_by FROM expenses WHERE client_request_id = ?", (request_id,)
            ).fetchone()
            if existing and existing["created_by"] == user["id"]:
                return {"id": existing["id"], "duplicate": True}
        raise
    log_audit(
        db, user["id"], "EXPENSE_CREATE", "expenses", cur.lastrowid,
        f"{category} {_money(data.amount)} at location {location_id or 'global'}",
        user["username"],
    )
    return {"id": cur.lastrowid}


@router.delete("/expenses/{expense_id}")
def delete_expense(
    expense_id: int,
    db=Depends(get_db),
    user=Depends(require_roles("super_admin", "zone_admin", "temple")),
):
    expense = db.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if not expense:
        raise HTTPException(404, "Expense not found")
    if expense["location_id"] is not None:
        if expense["location_id"] not in finance_location_ids(db, user):
            raise HTTPException(403, "Forbidden")
    elif user["role"] == "zone_admin":
        raise HTTPException(403, "Forbidden")
    db.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    db.commit()
    log_audit(
        db, user["id"], "EXPENSE_DELETE", "expenses", expense_id,
        f"{expense['category']} {expense['amount']}", user["username"],
    )
    return {"ok": True}
