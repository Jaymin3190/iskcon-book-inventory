from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import has_role, require_roles
from ..database import get_db
from ..services.scope import invoice_location_ids, location_ids, stock_location_ids, zone_location_ids
from ..services.timezone import now_ist

router = APIRouter(tags=["reports"])

SETTLED_SQL = "('paid','partial','unpaid')"


def _with_outstanding(rows):
    result = []
    for row in rows:
        data = dict(row)
        data["outstanding"] = max(
            0,
            round(float(data.get("revenue") or 0) - float(data.get("collected") or 0), 2),
        )
        result.append(data)
    return result


def _pending_cash_to_temple(db, zone_id, distributor_id=None):
    """Cash the zone team collected from customers but has not handed to the temple.

    Mirrors the Payment In tally: cash received on bills raised by the zone team
    (the zone admin or a distributor of that zone), minus handovers already
    recorded. Customer dues are reported separately as ``outstanding``.
    """
    if zone_id is None:
        return 0.0
    ids = zone_location_ids(db, zone_id)
    if not ids:
        return 0.0
    filters = ["i.location_id IN ({})".format(",".join("?" * len(ids)))]
    params = list(ids)
    if distributor_id is not None:
        filters.append(
            "(i.distributor_id = ? OR (i.distributor_id IS NULL AND i.cashier_id = ?))"
        )
        params.extend([distributor_id, distributor_id])
    row = db.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN paid > remitted
                                   THEN ROUND(paid - remitted, 2) ELSE 0 END),0) AS pending
              FROM (SELECT COALESCE((SELECT SUM(p.amount) FROM invoice_payments p
                                      WHERE p.invoice_id = i.id AND p.method = 'cash'),0) AS paid,
                           COALESCE((SELECT SUM(c.amount) FROM cash_submission_items c
                                      WHERE c.invoice_id = i.id),0) AS remitted
                      FROM invoices i
                      JOIN users u ON u.id = i.cashier_id
                     WHERE {' AND '.join(filters)}
                       AND u.role IN ('zone_admin','distributor')
                       AND u.zone_id = ?)""",
        params + [zone_id],
    ).fetchone()
    return round(float(row["pending"] or 0), 2)


@router.get("/api/reports/sales")
def sales_report(frm: str = None, to: str = None, location_id: int = None, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    ids = invoice_location_ids(db, user)
    sql = """SELECT i.location_id, l.code AS location_code, l.name AS location_name,
                    COUNT(*) AS invoice_count, COALESCE(SUM(i.total),0) AS revenue,
                    COALESCE(SUM(i.subtotal),0) AS subtotal, COALESCE(SUM(i.discount),0) AS discount,
                    COALESCE(SUM((SELECT COALESCE(SUM(p.amount),0) FROM invoice_payments p
                                   WHERE p.invoice_id = i.id)),0) AS collected
             FROM invoices i
             JOIN locations l ON l.id = i.location_id
             WHERE i.location_id IN ({}) AND i.status IN {settled}""".format(
                 ",".join("?" * len(ids)), settled=SETTLED_SQL)
    params = list(ids)
    if frm:
        sql += " AND datetime(i.created_at, '+5 hours', '+30 minutes') >= ?"
        params.append(frm)
    if to:
        sql += " AND datetime(i.created_at, '+5 hours', '+30 minutes') <= ?"
        params.append(to + " 23:59:59")
    if location_id:
        if location_id not in ids:
            return []
        sql += " AND i.location_id = ?"
        params.append(location_id)
    sql += " GROUP BY i.location_id"
    return _with_outstanding(db.execute(sql, params).fetchall())


@router.get("/api/reports/sales-by-book")
def sales_by_book(frm: str = None, to: str = None, location_id: int = None, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    ids = invoice_location_ids(db, user)
    if location_id is not None and location_id not in ids:
        return []
    sql = """SELECT b.id AS book_id, b.title, b.barcode,
                    COALESCE(SUM(ii.qty),0) AS qty_sold, COALESCE(SUM(ii.line_total),0) AS revenue
             FROM invoice_items ii
             JOIN invoices i ON i.id = ii.invoice_id
             JOIN books b ON b.id = ii.book_id
             WHERE i.location_id IN ({}) AND i.status IN ('paid','partial','unpaid')""".format(",".join("?" * len(ids)))
    params = list(ids)
    if location_id is not None:
        sql += " AND i.location_id = ?"
        params.append(location_id)
    if frm:
        sql += " AND datetime(i.created_at, '+5 hours', '+30 minutes') >= ?"
        params.append(frm)
    if to:
        sql += " AND datetime(i.created_at, '+5 hours', '+30 minutes') <= ?"
        params.append(to + " 23:59:59")
    sql += " GROUP BY b.id ORDER BY revenue DESC"
    return [dict(r) for r in db.execute(sql, params).fetchall()]


@router.get("/api/reports/distributor-sales")
def distributor_sales_report(frm: str = None, to: str = None, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    ids = invoice_location_ids(db, user)
    sql = """SELECT i.created_at AS sold_at, i.invoice_number,
                    u.name AS distributor_name, u.username AS distributor_username,
                    l.name AS location_name, b.title AS book_title, b.barcode,
                    ii.qty,
                    CASE WHEN i.razorpay_qr_code_id IS NOT NULL THEN 'razorpay'
                         ELSE i.payment_mode END AS payment_mode,
                    ii.line_total AS amount
             FROM invoices i
             JOIN users u ON u.id = COALESCE(i.distributor_id,
                    CASE WHEN (SELECT role FROM users WHERE id = i.cashier_id) = 'distributor'
                         THEN i.cashier_id END)
                          AND u.role = 'distributor'
             JOIN locations l ON l.id = i.location_id
             JOIN invoice_items ii ON ii.invoice_id = i.id
             JOIN books b ON b.id = ii.book_id
             WHERE i.location_id IN ({}) AND i.status IN ('paid','partial','unpaid')""".format(",".join("?" * len(ids)))
    params = list(ids)
    if has_role(user, "zone_admin"):
        sql += " AND u.zone_id = ?"
        params.append(user["zone_id"])
    if frm:
        sql += " AND datetime(i.created_at, '+5 hours', '+30 minutes') >= ?"
        params.append(frm)
    if to:
        sql += " AND datetime(i.created_at, '+5 hours', '+30 minutes') <= ?"
        params.append(to + " 23:59:59")
    sql += " ORDER BY i.created_at DESC, i.id DESC, ii.id"
    return [dict(r) for r in db.execute(sql, params).fetchall()]


@router.get("/api/reports/stock-summary")
def stock_summary(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    ids = stock_location_ids(db, user)
    sql = """SELECT l.id AS location_id, l.code, l.name, l.type, l.parent_id,
                    COUNT(s.book_id) AS skus, COALESCE(SUM(s.qty),0) AS total_units,
                    COALESCE(SUM(s.qty * b.price),0) AS stock_value
             FROM locations l
             LEFT JOIN stock s ON s.location_id = l.id
             LEFT JOIN books b ON b.id = s.book_id
             WHERE l.id IN ({})
             GROUP BY l.id ORDER BY l.type, l.name""".format(",".join("?" * len(ids)))
    return [dict(r) for r in db.execute(sql, list(ids)).fetchall()]


@router.get("/api/reports/stock-summary/{location_id}/books")
def stock_summary_books(location_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    ids = stock_location_ids(db, user)
    if location_id not in ids:
        raise HTTPException(403, "Forbidden")
    location = db.execute(
        "SELECT id, code, name, type FROM locations WHERE id = ?",
        (location_id,),
    ).fetchone()
    if not location:
        raise HTTPException(404, "Location not found")
    books = db.execute(
        """SELECT b.id AS book_id, b.title, b.barcode, b.isbn, b.price,
                  COALESCE(s.qty, 0) AS qty,
                  COALESCE(s.qty, 0) * b.price AS stock_value
           FROM books b
           LEFT JOIN stock s ON s.book_id = b.id AND s.location_id = ?
           ORDER BY b.title, b.barcode""",
        (location_id,),
    ).fetchall()
    return {
        "location": dict(location),
        "books": [dict(row) for row in books],
    }


@router.get("/api/reports/daily-zone-sales")
def daily_zone_sales(days: int = 14, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    """Books sold per IST day, broken down by zone, with totals."""
    days = max(1, min(days, 366))
    ids = location_ids(db, user)
    placeholders = ",".join("?" * len(ids))
    zones = [
        dict(row) for row in db.execute(
            f"""SELECT id, code, name FROM locations
                WHERE type = 'zone' AND id IN ({placeholders})
                ORDER BY name""",
            list(ids),
        ).fetchall()
    ]
    if not zones:
        return {"days": days, "start": None, "zones": [], "rows": [], "totals": {}, "total": 0}

    zone_ids = [zone["id"] for zone in zones]
    zone_placeholders = ",".join("?" * len(zone_ids))
    today = now_ist().date()
    start = (today - timedelta(days=days - 1)).isoformat()
    rows = db.execute(
        f"""WITH RECURSIVE tree(root_id, id) AS (
                SELECT id, id FROM locations WHERE type = 'zone'
                UNION ALL
                SELECT t.root_id, c.id FROM locations c JOIN tree t ON c.parent_id = t.id
            )
            SELECT date(i.created_at, '+5 hours', '+30 minutes') AS day,
                   t.root_id AS zone_id,
                   COALESCE(SUM(ii.qty), 0) AS units,
                   COUNT(DISTINCT i.id) AS invoice_count
              FROM invoices i
              JOIN tree t ON t.id = i.location_id
              JOIN invoice_items ii ON ii.invoice_id = i.id
             WHERE i.status IN ('paid','partial','unpaid')
               AND t.root_id IN ({zone_placeholders})
               AND date(i.created_at, '+5 hours', '+30 minutes') >= ?
             GROUP BY day, t.root_id
             ORDER BY day DESC, t.root_id""",
        zone_ids + [start],
    ).fetchall()

    by_day = {}
    zone_totals = {zone["id"]: 0 for zone in zones}
    for row in rows:
        entry = by_day.setdefault(row["day"], {
            "date": row["day"],
            "total": 0,
            "invoice_count": 0,
            "zones": {},
        })
        units = int(row["units"] or 0)
        entry["zones"][str(row["zone_id"])] = units
        entry["total"] += units
        entry["invoice_count"] += int(row["invoice_count"] or 0)
        zone_totals[row["zone_id"]] += units

    # Include quiet days too so the table is a true daily series.
    daily_rows = []
    for offset in range(days):
        day = (today - timedelta(days=offset)).isoformat()
        daily_rows.append(by_day.get(day) or {
            "date": day,
            "total": 0,
            "invoice_count": 0,
            "zones": {},
        })

    return {
        "days": days,
        "start": start,
        "zones": zones,
        "rows": daily_rows,
        "totals": {str(zone_id): units for zone_id, units in zone_totals.items()},
        "total": sum(zone_totals.values()),
    }


@router.get("/api/reports/zones")
def zone_overview(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    ids = location_ids(db, user)
    sql = """WITH RECURSIVE tree(root_id, id) AS (
                   SELECT id, id FROM locations WHERE type = 'zone'
                   UNION ALL
                   SELECT t.root_id, c.id FROM locations c JOIN tree t ON c.parent_id = t.id
               ),
               invoice_agg AS (
                   SELECT t.root_id,
                          COUNT(*) AS invoice_count,
                          COALESCE(SUM(i.total),0) AS revenue,
                          COALESCE(SUM((SELECT COALESCE(SUM(p.amount),0) FROM invoice_payments p
                                         WHERE p.invoice_id = i.id)),0) AS collected,
                          COALESCE(SUM(CASE WHEN i.payment_mode = 'cash' THEN i.total ELSE 0 END),0) AS cash_total,
                          COALESCE(SUM(CASE WHEN i.payment_mode = 'upi' AND i.razorpay_qr_code_id IS NULL THEN i.total ELSE 0 END),0) AS upi_total,
                          COALESCE(SUM(CASE WHEN i.razorpay_qr_code_id IS NOT NULL THEN i.total ELSE 0 END),0) AS razorpay_total,
                          COALESCE(SUM(CASE WHEN i.payment_mode = 'ledger' THEN i.total ELSE 0 END),0) AS ledger_total
                     FROM invoices i JOIN tree t ON t.id = i.location_id
                    WHERE i.status IN ('paid','partial','unpaid')
                    GROUP BY t.root_id
               ),
               sold_agg AS (
                   SELECT t.root_id, COALESCE(SUM(ii.qty),0) AS books_sold
                     FROM invoices i
                     JOIN tree t ON t.id = i.location_id
                     JOIN invoice_items ii ON ii.invoice_id = i.id
                    WHERE i.status IN ('paid','partial','unpaid')
                    GROUP BY t.root_id
               )
             SELECT l.id, l.code, l.name,
                    (SELECT COUNT(*) FROM stock s WHERE s.location_id = l.id AND s.qty > 0) AS stock_skus,
                    (SELECT COALESCE(SUM(s.qty),0) FROM stock s WHERE s.location_id = l.id) AS stock_units,
                    (SELECT COALESCE(SUM(s.qty * b.price),0)
                       FROM stock s JOIN books b ON b.id = s.book_id
                      WHERE s.location_id = l.id) AS stock_value,
                    (SELECT COUNT(*) FROM users u
                      WHERE u.role = 'distributor' AND u.zone_id = l.id AND u.is_active = 1) AS distributor_count,
                    COALESCE(ia.invoice_count,0) AS invoice_count,
                    COALESCE(ia.revenue,0) AS revenue,
                    COALESCE(ia.collected,0) AS collected,
                    COALESCE(ia.revenue,0) - COALESCE(ia.collected,0) AS outstanding,
                    COALESCE(ia.cash_total,0) AS cash_total,
                    COALESCE(ia.upi_total,0) AS upi_total,
                    COALESCE(ia.razorpay_total,0) AS razorpay_total,
                    COALESCE(ia.ledger_total,0) AS ledger_total,
                    COALESCE(sa.books_sold,0) AS books_sold
             FROM locations l
             LEFT JOIN invoice_agg ia ON ia.root_id = l.id
             LEFT JOIN sold_agg sa ON sa.root_id = l.id
             WHERE l.type = 'zone' AND l.id IN ({})
             ORDER BY l.name""".format(",".join("?" * len(ids)))
    rows = [dict(r) for r in db.execute(sql, list(ids)).fetchall()]
    for row in rows:
        row["pending_cash"] = _pending_cash_to_temple(db, row["id"])
    return rows


@router.get("/api/reports/zones/{zone_id}/invoices")
def zone_invoices(zone_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    if not has_role(user, "super_admin") and user["zone_id"] != zone_id:
        raise HTTPException(403, "Forbidden")
    zone = db.execute("SELECT id FROM locations WHERE id = ? AND type = 'zone'", (zone_id,)).fetchone()
    if not zone:
        raise HTTPException(404, "Zone not found")
    ids = zone_location_ids(db, zone_id)
    sql = """SELECT i.id, i.invoice_number, i.created_at, i.customer_name, i.customer_phone,
                    CASE WHEN i.razorpay_qr_code_id IS NOT NULL THEN 'razorpay'
                         ELSE i.payment_mode END AS payment_mode,
                    i.payment_ref, i.status, i.total, i.distributor_id,
                    COALESCE((SELECT SUM(p.amount) FROM invoice_payments p
                               WHERE p.invoice_id = i.id),0) AS amount_paid,
                    l.name AS location_name,
                    u.name AS cashier_name, u.username AS cashier_username,
                    u.role AS cashier_role,
                    (SELECT COALESCE(SUM(ii.qty),0) FROM invoice_items ii WHERE ii.invoice_id = i.id) AS books_qty
             FROM invoices i
             JOIN locations l ON l.id = i.location_id
             LEFT JOIN users u ON u.id = i.cashier_id
             WHERE i.location_id IN ({})
             ORDER BY i.id DESC""".format(",".join("?" * len(ids)))
    rows = []
    for row in db.execute(sql, list(ids)).fetchall():
        data = dict(row)
        data["balance_due"] = max(
            0, round(float(data["total"] or 0) - float(data["amount_paid"] or 0), 2)
        )
        rows.append(data)
    return rows


@router.get("/api/reports/distributors")
def distributors_report(zone_id: int = None, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    ids = location_ids(db, user)
    if zone_id is not None:
        if zone_id not in ids:
            raise HTTPException(403, "Forbidden")
    elif has_role(user, "zone_admin"):
        zone_id = user["zone_id"]

    where = "u.role = 'distributor'"
    params = []
    if zone_id is not None:
        where += " AND u.zone_id = ?"
        params.append(zone_id)

    sql = f"""SELECT u.id, u.name, u.username, u.is_active, u.zone_id, u.created_at,
                     l.name AS zone_name,
                     (SELECT COUNT(*) FROM invoices i
                       WHERE (i.distributor_id = u.id OR (i.distributor_id IS NULL AND i.cashier_id = u.id))
                         AND i.status IN {SETTLED_SQL}) AS invoice_count,
                     (SELECT COALESCE(SUM(i.total),0) FROM invoices i
                       WHERE (i.distributor_id = u.id OR (i.distributor_id IS NULL AND i.cashier_id = u.id))
                         AND i.status IN {SETTLED_SQL}) AS revenue,
                     (SELECT COALESCE(SUM((SELECT COALESCE(SUM(p.amount),0) FROM invoice_payments p
                                            WHERE p.invoice_id = i.id)),0) FROM invoices i
                       WHERE (i.distributor_id = u.id OR (i.distributor_id IS NULL AND i.cashier_id = u.id))
                         AND i.status IN {SETTLED_SQL}) AS collected,
                     (SELECT COALESCE(SUM(i.total),0) FROM invoices i
                       WHERE (i.distributor_id = u.id OR (i.distributor_id IS NULL AND i.cashier_id = u.id))
                         AND i.payment_mode = 'cash' AND i.status IN {SETTLED_SQL}) AS cash_total,
                     (SELECT COALESCE(SUM(i.total),0) FROM invoices i
                       WHERE (i.distributor_id = u.id OR (i.distributor_id IS NULL AND i.cashier_id = u.id))
                         AND i.payment_mode = 'upi'
                         AND i.razorpay_qr_code_id IS NULL AND i.status IN {SETTLED_SQL}) AS upi_total,
                     (SELECT COALESCE(SUM(i.total),0) FROM invoices i
                       WHERE (i.distributor_id = u.id OR (i.distributor_id IS NULL AND i.cashier_id = u.id))
                         AND i.razorpay_qr_code_id IS NOT NULL
                         AND i.status IN {SETTLED_SQL}) AS razorpay_total,
                     (SELECT COALESCE(SUM(i.total),0) FROM invoices i
                       WHERE (i.distributor_id = u.id OR (i.distributor_id IS NULL AND i.cashier_id = u.id))
                         AND i.payment_mode = 'ledger' AND i.status IN {SETTLED_SQL}) AS ledger_total,
                     (SELECT COALESCE(SUM(ii.qty),0)
                        FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id
                       WHERE (i.distributor_id = u.id OR (i.distributor_id IS NULL AND i.cashier_id = u.id))
                         AND i.status IN {SETTLED_SQL}) AS books_sold,
                     (SELECT MAX(i.created_at) FROM invoices i
                       WHERE (i.distributor_id = u.id OR (i.distributor_id IS NULL AND i.cashier_id = u.id))
                         AND i.status IN {SETTLED_SQL}) AS last_sale
              FROM users u
              LEFT JOIN locations l ON l.id = u.zone_id
              WHERE {where}
              ORDER BY l.name, u.name"""
    rows = []
    for row in db.execute(sql, params).fetchall():
        data = dict(row)
        data["outstanding"] = max(
            0, round(float(data["revenue"] or 0) - float(data["collected"] or 0), 2)
        )
        data["pending_to_temple"] = _pending_cash_to_temple(db, data["zone_id"], data["id"])
        rows.append(data)
    return rows


@router.get("/api/reports/distributors/{user_id}")
def distributor_details(user_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    row = db.execute(
        """SELECT u.id, u.name, u.username, u.role, u.is_active, u.zone_id, u.created_at,
                  z.name AS zone_name
           FROM users u LEFT JOIN locations z ON z.id = u.zone_id
           WHERE u.id = ?""",
        (user_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "User not found")
    if row["role"] != "distributor":
        raise HTTPException(400, "Selected user is not a distributor")
    if not has_role(user, "super_admin") and row["zone_id"] != user["zone_id"]:
        raise HTTPException(403, "Forbidden")

    summary = db.execute(
        f"""SELECT COUNT(*) AS invoice_count,
                  COALESCE(SUM(total),0) AS revenue,
                  COALESCE(SUM(subtotal),0) AS subtotal,
                  COALESCE(SUM(discount),0) AS discount,
                  COALESCE(SUM((SELECT COALESCE(SUM(p.amount),0) FROM invoice_payments p
                                 WHERE p.invoice_id = invoices.id)),0) AS collected,
                  COALESCE(SUM(CASE WHEN payment_mode = 'cash' THEN total ELSE 0 END),0) AS cash_total,
                  COALESCE(SUM(CASE WHEN payment_mode = 'upi' AND razorpay_qr_code_id IS NULL THEN total ELSE 0 END),0) AS upi_total,
                  COALESCE(SUM(CASE WHEN razorpay_qr_code_id IS NOT NULL THEN total ELSE 0 END),0) AS razorpay_total,
                  COALESCE(SUM(CASE WHEN payment_mode = 'ledger' THEN total ELSE 0 END),0) AS ledger_total,
                  COALESCE((SELECT SUM(ii.qty) FROM invoice_items ii
                              JOIN invoices i2 ON i2.id = ii.invoice_id
                             WHERE (i2.distributor_id = ? OR (i2.distributor_id IS NULL AND i2.cashier_id = ?))
                               AND i2.status IN {SETTLED_SQL}),0) AS books_sold,
                  MAX(created_at) AS last_sale
           FROM invoices
           WHERE (distributor_id = ? OR (distributor_id IS NULL AND cashier_id = ?))
             AND status IN {SETTLED_SQL}""",
        (user_id, user_id, user_id, user_id),
    ).fetchone()

    invoices = db.execute(
        """SELECT i.id, i.invoice_number, i.created_at, i.customer_name, i.customer_phone,
                  CASE WHEN i.razorpay_qr_code_id IS NOT NULL THEN 'razorpay'
                       ELSE i.payment_mode END AS payment_mode,
                  i.payment_ref, i.status, i.subtotal, i.discount, i.total,
                  COALESCE((SELECT SUM(p.amount) FROM invoice_payments p
                             WHERE p.invoice_id = i.id),0) AS amount_paid,
                  l.name AS location_name,
                  (SELECT COUNT(*) FROM invoice_items ii WHERE ii.invoice_id = i.id) AS line_count,
                  (SELECT COALESCE(SUM(ii.qty),0) FROM invoice_items ii WHERE ii.invoice_id = i.id) AS books_qty
           FROM invoices i JOIN locations l ON l.id = i.location_id
           WHERE (i.distributor_id = ? OR (i.distributor_id IS NULL AND i.cashier_id = ?))
           ORDER BY i.id DESC""",
        (user_id, user_id),
    ).fetchall()

    books = db.execute(
        f"""SELECT b.id AS book_id, b.title, b.barcode,
                  COALESCE(SUM(ii.qty),0) AS qty,
                  COALESCE(SUM(ii.line_total),0) AS revenue
           FROM invoice_items ii
           JOIN invoices i ON i.id = ii.invoice_id
           JOIN books b ON b.id = ii.book_id
           WHERE (i.distributor_id = ? OR (i.distributor_id IS NULL AND i.cashier_id = ?))
             AND i.status IN {SETTLED_SQL}
           GROUP BY b.id ORDER BY revenue DESC""",
        (user_id, user_id),
    ).fetchall()

    summary_data = dict(summary)
    summary_data["outstanding"] = max(
        0,
        round(float(summary_data["revenue"] or 0) - float(summary_data["collected"] or 0), 2),
    )
    summary_data["pending_to_temple"] = _pending_cash_to_temple(db, row["zone_id"], user_id)
    invoice_rows = []
    for invoice in invoices:
        data = dict(invoice)
        data["balance_due"] = max(
            0, round(float(data["total"] or 0) - float(data["amount_paid"] or 0), 2)
        )
        invoice_rows.append(data)

    return {
        "distributor": dict(row),
        "summary": summary_data,
        "invoices": invoice_rows,
        "books": [dict(r) for r in books],
    }


@router.get("/api/reports/transfers")
def transfers_report(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    if user["role"] == "zone_admin":
        ids = zone_location_ids(db, user["zone_id"])
        scope_sql = "to_location_id IN ({})".format(",".join("?" * len(ids)))
        scope_params = list(ids)
    else:
        ids = location_ids(db, user)
        placeholders = ",".join("?" * len(ids))
        scope_sql = "(from_location_id IN ({}) OR to_location_id IN ({}))".format(placeholders, placeholders)
        scope_params = list(ids) + list(ids)
    sql = """SELECT status, COUNT(*) AS n
             FROM transfers
             WHERE {}
             GROUP BY status""".format(scope_sql)
    return [dict(r) for r in db.execute(sql, scope_params).fetchall()]


@router.get("/api/reports/transfers/{status}/details")
def transfer_status_details(status: str, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    if user["role"] == "zone_admin":
        ids = zone_location_ids(db, user["zone_id"])
        scope_sql = "t.to_location_id IN ({})".format(",".join("?" * len(ids)))
        scope_params = list(ids)
    else:
        ids = location_ids(db, user)
        placeholders = ",".join("?" * len(ids))
        scope_sql = "(t.from_location_id IN ({}) OR t.to_location_id IN ({}))".format(placeholders, placeholders)
        scope_params = list(ids) + list(ids)
    rows = db.execute(
        """SELECT t.id AS transfer_id, t.request_number, t.status, t.requested_at,
                  src.name AS from_location, dest.name AS to_location,
                  b.id AS book_id, b.title, b.barcode, ti.qty
           FROM transfers t
           JOIN locations src ON src.id = t.from_location_id
           JOIN locations dest ON dest.id = t.to_location_id
           LEFT JOIN transfer_items ti ON ti.transfer_id = t.id
           LEFT JOIN books b ON b.id = ti.book_id
           WHERE t.status = ? AND {}
           ORDER BY t.requested_at DESC, t.id DESC, b.title""".format(scope_sql),
        [status] + scope_params,
    ).fetchall()
    transfers_by_id = {}
    book_summary = {}
    total_units = 0
    for row in rows:
        transfer = transfers_by_id.setdefault(row["transfer_id"], {
            "transfer_id": row["transfer_id"],
            "request_number": row["request_number"],
            "requested_at": row["requested_at"],
            "from_location": row["from_location"],
            "to_location": row["to_location"],
            "items": [],
        })
        if row["book_id"] is None:
            continue
        item = {
            "book_id": row["book_id"],
            "title": row["title"],
            "barcode": row["barcode"],
            "qty": row["qty"],
        }
        transfer["items"].append(item)
        summary = book_summary.setdefault(row["book_id"], {
            "book_id": row["book_id"],
            "title": row["title"],
            "barcode": row["barcode"],
            "qty": 0,
            "transfer_count": 0,
            "_transfer_ids": set(),
        })
        summary["qty"] += row["qty"]
        summary["_transfer_ids"].add(row["transfer_id"])
        total_units += row["qty"]
    summary_rows = []
    for summary in book_summary.values():
        summary["transfer_count"] = len(summary.pop("_transfer_ids"))
        summary_rows.append(summary)
    summary_rows.sort(key=lambda item: item["title"].casefold())
    return {
        "status": status,
        "transfer_count": len(transfers_by_id),
        "total_units": total_units,
        "books": summary_rows,
        "transfers": list(transfers_by_id.values()),
    }


@router.get("/api/reports/returns")
def returns_report(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    ids = location_ids(db, user)
    sql = """SELECT r.status, COUNT(*) AS n, COALESCE(SUM(ri.qty),0) AS units
             FROM returns r
             LEFT JOIN return_items ri ON ri.return_id = r.id
             WHERE r.location_id IN ({})
             GROUP BY r.status""".format(",".join("?" * len(ids)))
    return [dict(r) for r in db.execute(sql, list(ids)).fetchall()]


class AuditFilter(BaseModel):
    limit: int = 100


def _audit_scope(user):
    if has_role(user, "zone_admin"):
        return "(a.action = 'LOGIN' OR a.username = ? OR a.user_id = ?)", [user["username"], user["id"]]
    return None, []


def _audit_where(user, q=None, action=None, entity=None, username=None, frm=None, to=None):
    where = []
    params = []
    scope, scope_params = _audit_scope(user)
    if scope:
        where.append(scope)
        params.extend(scope_params)
    if q and q.strip():
        like = f"%{q.strip()}%"
        where.append("(a.username LIKE ? OR a.action LIKE ? OR a.entity LIKE ? OR a.details LIKE ?)")
        params.extend([like, like, like, like])
    if action:
        where.append("a.action = ?")
        params.append(action)
    if entity:
        where.append("a.entity = ?")
        params.append(entity)
    if username:
        where.append("a.username = ?")
        params.append(username)
    if frm:
        where.append("date(a.created_at, '+5 hours', '+30 minutes') >= date(?)")
        params.append(frm)
    if to:
        where.append("date(a.created_at, '+5 hours', '+30 minutes') <= date(?)")
        params.append(to)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return clause, params


@router.get("/api/audit")
def audit_trail(limit: int = 100, offset: int = 0, q: str = None, action: str = None,
                entity: str = None, username: str = None, frm: str = None, to: str = None,
                db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    clause, params = _audit_where(user, q, action, entity, username, frm, to)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    sql = f"SELECT a.* FROM audit_logs a{clause} ORDER BY a.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return [dict(r) for r in db.execute(sql, params).fetchall()]


@router.get("/api/audit/options")
def audit_options(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    clause, params = _audit_scope(user)
    where = f" WHERE {clause}" if clause else ""
    actions = [
        dict(r) for r in db.execute(
            f"SELECT a.action, COUNT(*) AS n FROM audit_logs a{where} GROUP BY a.action ORDER BY a.action",
            params,
        ).fetchall()
    ]
    users = [
        dict(r) for r in db.execute(
            f"SELECT a.username, COUNT(*) AS n FROM audit_logs a{where}"
            f"{' AND' if where else ' WHERE'} a.username IS NOT NULL AND a.username != '' "
            "GROUP BY a.username ORDER BY a.username",
            params,
        ).fetchall()
    ]
    return {"actions": actions, "users": users}
