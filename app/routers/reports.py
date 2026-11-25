from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import has_role, require_roles
from ..database import get_db
from ..services.scope import (
    finance_location_ids,
    invoice_location_ids,
    location_ids,
    stock_location_ids,
    zone_location_ids,
)
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


def _pending_cash_to_temple(db, zone_id, distributor_id=None, role=None):
    """Cash the zone team collected from customers but has not handed to the temple.

    Mirrors the Payment In tally: cash received on bills raised by the zone team
    (the zone admin or a distributor of that zone), minus handovers already
    recorded. Customer dues are reported separately as ``outstanding``.

    A zone leader can act as the distributor on a bill. Only bills explicitly
    credited to that leader count there; their ordinary walk-in bills are zone
    cash, not a leader payable.
    """
    if zone_id is None:
        return 0.0
    ids = zone_location_ids(db, zone_id)
    if not ids:
        return 0.0
    filters = ["i.location_id IN ({})".format(",".join("?" * len(ids)))]
    params = list(ids)
    if distributor_id is not None:
        if role is None:
            owner = db.execute(
                "SELECT role FROM users WHERE id = ?", (distributor_id,)
            ).fetchone()
            role = owner["role"] if owner else None
        if role == "distributor":
            filters.append(
                "(i.distributor_id = ? OR (i.distributor_id IS NULL AND i.cashier_id = ?))"
            )
            params.extend([distributor_id, distributor_id])
        else:
            filters.append("i.distributor_id = ?")
            params.append(distributor_id)
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


@router.get("/api/reports/profit-loss")
def profit_loss_report(
    frm: str = None,
    to: str = None,
    location_id: int = None,
    db=Depends(get_db),
    user=Depends(require_roles("super_admin", "zone_admin", "temple")),
):
    """Revenue, cost of goods sold, running expenses and the resulting profit.

    Purchases are deliberately *not* an expense here: buying books only moves
    value from cash into stock. The cost becomes an expense only when a copy is
    sold (COGS), damaged or written off.
    """
    def money(value):
        return round(float(value or 0), 2)

    def pct(part, whole):
        return round(part / whole * 100, 2) if whole else 0.0

    ids = finance_location_ids(db, user)
    if location_id is not None:
        if location_id not in ids:
            raise HTTPException(403, "Forbidden")
        ids = {location_id}
    id_list = sorted(i for i in ids if i is not None)

    def empty_report():
        return {
            "from": frm,
            "to": to,
            "revenue": {"invoice_count": 0, "gross_sales": 0, "discounts": 0,
                        "net_revenue": 0},
            "cogs": 0,
            "gross_profit": 0,
            "gross_margin_pct": 0,
            "expenses": {"total": 0, "by_category": []},
            "stock_loss": {"damaged_units": 0, "damaged_value": 0},
            "net_profit": 0,
            "net_margin_pct": 0,
            "purchases": {"bills": 0, "value": 0, "paid": 0, "balance": 0},
            "locations": [],
            "books": [],
            "missing_cost_books": [],
        }

    if not id_list:
        return empty_report()

    ph = ",".join("?" * len(id_list))
    inv_where = f"i.location_id IN ({ph}) AND i.status IN ('paid','partial','unpaid')"
    inv_params = list(id_list)
    if frm:
        inv_where += " AND datetime(i.created_at, '+5 hours', '+30 minutes') >= ?"
        inv_params.append(frm)
    if to:
        inv_where += " AND datetime(i.created_at, '+5 hours', '+30 minutes') <= ?"
        inv_params.append(to + " 23:59:59")

    revenue_row = db.execute(
        f"""SELECT COUNT(*) AS invoice_count,
                   COALESCE(SUM(i.subtotal),0) AS gross_sales,
                   COALESCE(SUM(i.discount),0) AS discounts,
                   COALESCE(SUM(i.total),0) AS net_revenue
              FROM invoices i WHERE {inv_where}""",
        inv_params,
    ).fetchone()

    cost_expr = "ii.qty * CASE WHEN ii.unit_cost > 0 THEN ii.unit_cost ELSE COALESCE(b.cost_price,0) END"
    cogs_row = db.execute(
        f"""SELECT COALESCE(SUM({cost_expr}),0) AS cogs,
                   COALESCE(SUM(CASE WHEN COALESCE(ii.unit_cost,0) <= 0
                                           AND COALESCE(b.cost_price,0) <= 0
                                     THEN ii.qty ELSE 0 END),0) AS uncosted_units
              FROM invoice_items ii
              JOIN invoices i ON i.id = ii.invoice_id
              JOIN books b ON b.id = ii.book_id
             WHERE {inv_where}""",
        inv_params,
    ).fetchone()

    missing = db.execute(
        f"""SELECT b.title, b.barcode, COALESCE(SUM(ii.qty),0) AS units
              FROM invoice_items ii
              JOIN invoices i ON i.id = ii.invoice_id
              JOIN books b ON b.id = ii.book_id
             WHERE {inv_where}
               AND COALESCE(ii.unit_cost,0) <= 0
               AND COALESCE(b.cost_price,0) <= 0
             GROUP BY b.id ORDER BY units DESC""",
        inv_params,
    ).fetchall()

    if location_id is not None:
        expense_where = "e.location_id = ?"
        expense_params = [location_id]
    elif user["role"] in ("super_admin", "temple"):
        expense_where = f"(e.location_id IN ({ph}) OR e.location_id IS NULL)"
        expense_params = list(id_list)
    else:
        expense_where = f"e.location_id IN ({ph})"
        expense_params = list(id_list)
    if frm:
        expense_where += " AND e.expense_date >= ?"
        expense_params.append(frm)
    if to:
        expense_where += " AND e.expense_date <= ?"
        expense_params.append(to)

    expense_categories = db.execute(
        f"""SELECT e.category, COALESCE(SUM(e.amount),0) AS amount
              FROM expenses e WHERE {expense_where}
             GROUP BY e.category ORDER BY amount DESC""",
        expense_params,
    ).fetchall()
    expense_total = round(sum(float(r["amount"] or 0) for r in expense_categories), 2)

    damaged_where = f"d.location_id IN ({ph})"
    damaged_params = list(id_list)
    if frm:
        damaged_where += " AND datetime(d.created_at, '+5 hours', '+30 minutes') >= ?"
        damaged_params.append(frm)
    if to:
        damaged_where += " AND datetime(d.created_at, '+5 hours', '+30 minutes') <= ?"
        damaged_params.append(to + " 23:59:59")
    damaged_row = db.execute(
        f"""SELECT COALESCE(SUM(d.qty),0) AS units,
                   COALESCE(SUM(d.qty * COALESCE(b.cost_price,0)),0) AS value
              FROM damaged d JOIN books b ON b.id = d.book_id
             WHERE {damaged_where}""",
        damaged_params,
    ).fetchone()

    purchase_where = f"p.location_id IN ({ph})"
    purchase_params = list(id_list)
    if frm:
        purchase_where += " AND p.purchase_date >= ?"
        purchase_params.append(frm)
    if to:
        purchase_where += " AND p.purchase_date <= ?"
        purchase_params.append(to)
    purchase_row = db.execute(
        f"""SELECT COUNT(*) AS bills, COALESCE(SUM(p.total),0) AS value,
                   COALESCE(SUM(p.amount_paid),0) AS paid
              FROM purchases p WHERE {purchase_where}""",
        purchase_params,
    ).fetchone()

    # Per-location roll-up
    location_names = {
        r["id"]: r["name"]
        for r in db.execute(
            f"SELECT id, name FROM locations WHERE id IN ({ph})", id_list
        ).fetchall()
    }
    by_location = {
        lid: {"location_id": lid, "location_name": location_names.get(lid, ""),
              "revenue": 0.0, "cogs": 0.0, "expenses": 0.0, "stock_loss": 0.0}
        for lid in id_list
    }

    for row in db.execute(
        f"""SELECT i.location_id, COALESCE(SUM(i.total),0) AS revenue
              FROM invoices i WHERE {inv_where} GROUP BY i.location_id""",
        inv_params,
    ).fetchall():
        if row["location_id"] in by_location:
            by_location[row["location_id"]]["revenue"] = money(row["revenue"])

    for row in db.execute(
        f"""SELECT i.location_id, COALESCE(SUM({cost_expr}),0) AS cogs
              FROM invoice_items ii
              JOIN invoices i ON i.id = ii.invoice_id
              JOIN books b ON b.id = ii.book_id
             WHERE {inv_where} GROUP BY i.location_id""",
        inv_params,
    ).fetchall():
        if row["location_id"] in by_location:
            by_location[row["location_id"]]["cogs"] = money(row["cogs"])

    for row in db.execute(
        f"""SELECT e.location_id, COALESCE(SUM(e.amount),0) AS amount
              FROM expenses e WHERE {expense_where} GROUP BY e.location_id""",
        expense_params,
    ).fetchall():
        if row["location_id"] in by_location:
            by_location[row["location_id"]]["expenses"] = money(row["amount"])

    for row in db.execute(
        f"""SELECT d.location_id, COALESCE(SUM(d.qty * COALESCE(b.cost_price,0)),0) AS value
              FROM damaged d JOIN books b ON b.id = d.book_id
             WHERE {damaged_where} GROUP BY d.location_id""",
        damaged_params,
    ).fetchall():
        if row["location_id"] in by_location:
            by_location[row["location_id"]]["stock_loss"] = money(row["value"])

    locations = []
    for row in by_location.values():
        gross = round(row["revenue"] - row["cogs"], 2)
        row["gross_profit"] = gross
        row["net_profit"] = round(gross - row["expenses"] - row["stock_loss"], 2)
        locations.append(row)
    locations.sort(key=lambda r: r["net_profit"], reverse=True)

    book_rows = db.execute(
        f"""SELECT b.id AS book_id, b.title, b.barcode,
                   COALESCE(SUM(ii.qty),0) AS qty_sold,
                   COALESCE(SUM(ii.line_total),0) AS revenue,
                   COALESCE(SUM({cost_expr}),0) AS cogs
              FROM invoice_items ii
              JOIN invoices i ON i.id = ii.invoice_id
              JOIN books b ON b.id = ii.book_id
             WHERE {inv_where}
             GROUP BY b.id ORDER BY revenue DESC LIMIT 100""",
        inv_params,
    ).fetchall()
    books = []
    for row in book_rows:
        revenue = money(row["revenue"])
        cogs = money(row["cogs"])
        profit = round(revenue - cogs, 2)
        books.append({
            "book_id": row["book_id"],
            "title": row["title"],
            "barcode": row["barcode"],
            "qty_sold": int(row["qty_sold"] or 0),
            "revenue": revenue,
            "cogs": cogs,
            "gross_profit": profit,
            "margin_pct": pct(profit, revenue),
        })

    net_revenue = money(revenue_row["net_revenue"])
    cogs = money(cogs_row["cogs"])
    gross_profit = round(net_revenue - cogs, 2)
    stock_loss = money(damaged_row["value"])
    net_profit = round(gross_profit - expense_total - stock_loss, 2)

    return {
        "from": frm,
        "to": to,
        "revenue": {
            "invoice_count": int(revenue_row["invoice_count"] or 0),
            "gross_sales": money(revenue_row["gross_sales"]),
            "discounts": money(revenue_row["discounts"]),
            "net_revenue": net_revenue,
        },
        "cogs": cogs,
        "gross_profit": gross_profit,
        "gross_margin_pct": pct(gross_profit, net_revenue),
        "expenses": {
            "total": expense_total,
            "by_category": [
                {"category": r["category"], "amount": money(r["amount"])}
                for r in expense_categories
            ],
        },
        "stock_loss": {
            "damaged_units": int(damaged_row["units"] or 0),
            "damaged_value": stock_loss,
        },
        "net_profit": net_profit,
        "net_margin_pct": pct(net_profit, net_revenue),
        "purchases": {
            "bills": int(purchase_row["bills"] or 0),
            "value": money(purchase_row["value"]),
            "paid": money(purchase_row["paid"]),
            "balance": max(0, money(float(purchase_row["value"] or 0) - float(purchase_row["paid"] or 0))),
        },
        "locations": locations,
        "books": books,
        "missing_cost_books": [
            {"title": r["title"], "barcode": r["barcode"], "units": int(r["units"] or 0)}
            for r in missing
        ],
    }


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
                          AND u.role IN ('distributor', 'zone_admin')
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
                      WHERE u.zone_id = l.id AND u.is_active = 1
                        AND (u.role = 'distributor' OR (
                            u.role = 'zone_admin' AND EXISTS (
                                SELECT 1 FROM invoices ix
                                 WHERE ix.distributor_id = u.id
                                   AND ix.status IN ('paid','partial','unpaid')
                            )
                        ))) AS distributor_count,
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

    # A zone leader may also distribute books. Show them once they have at least
    # one bill credited to them, so ordinary zone leaders do not clutter the list.
    where = f"""(u.role = 'distributor' OR (
                    u.role = 'zone_admin' AND EXISTS (
                        SELECT 1 FROM invoices ix
                         WHERE ix.distributor_id = u.id
                           AND ix.status IN {SETTLED_SQL}
                    )
                ))"""
    params = []
    if zone_id is not None:
        where += " AND u.zone_id = ?"
        params.append(zone_id)

    sql = f"""SELECT u.id, u.name, u.username, u.role, u.is_active, u.zone_id, u.created_at,
                     l.name AS zone_name,
                     (SELECT COUNT(*) FROM invoices i
                       WHERE COALESCE(i.distributor_id, CASE WHEN u.role = 'distributor' THEN i.cashier_id END) = u.id
                         AND i.status IN {SETTLED_SQL}) AS invoice_count,
                     (SELECT COALESCE(SUM(i.total),0) FROM invoices i
                       WHERE COALESCE(i.distributor_id, CASE WHEN u.role = 'distributor' THEN i.cashier_id END) = u.id
                         AND i.status IN {SETTLED_SQL}) AS revenue,
                     (SELECT COALESCE(SUM((SELECT COALESCE(SUM(p.amount),0) FROM invoice_payments p
                                            WHERE p.invoice_id = i.id)),0) FROM invoices i
                       WHERE COALESCE(i.distributor_id, CASE WHEN u.role = 'distributor' THEN i.cashier_id END) = u.id
                         AND i.status IN {SETTLED_SQL}) AS collected,
                     (SELECT COALESCE(SUM(i.total),0) FROM invoices i
                       WHERE COALESCE(i.distributor_id, CASE WHEN u.role = 'distributor' THEN i.cashier_id END) = u.id
                         AND i.payment_mode = 'cash' AND i.status IN {SETTLED_SQL}) AS cash_total,
                     (SELECT COALESCE(SUM(i.total),0) FROM invoices i
                       WHERE COALESCE(i.distributor_id, CASE WHEN u.role = 'distributor' THEN i.cashier_id END) = u.id
                         AND i.payment_mode = 'upi'
                         AND i.razorpay_qr_code_id IS NULL AND i.status IN {SETTLED_SQL}) AS upi_total,
                     (SELECT COALESCE(SUM(i.total),0) FROM invoices i
                       WHERE COALESCE(i.distributor_id, CASE WHEN u.role = 'distributor' THEN i.cashier_id END) = u.id
                         AND i.razorpay_qr_code_id IS NOT NULL
                         AND i.status IN {SETTLED_SQL}) AS razorpay_total,
                     (SELECT COALESCE(SUM(i.total),0) FROM invoices i
                       WHERE COALESCE(i.distributor_id, CASE WHEN u.role = 'distributor' THEN i.cashier_id END) = u.id
                         AND i.payment_mode = 'ledger' AND i.status IN {SETTLED_SQL}) AS ledger_total,
                     (SELECT COALESCE(SUM(ii.qty),0)
                        FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id
                       WHERE COALESCE(i.distributor_id, CASE WHEN u.role = 'distributor' THEN i.cashier_id END) = u.id
                         AND i.status IN {SETTLED_SQL}) AS books_sold,
                     (SELECT MAX(i.created_at) FROM invoices i
                       WHERE COALESCE(i.distributor_id, CASE WHEN u.role = 'distributor' THEN i.cashier_id END) = u.id
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
        data["pending_to_temple"] = _pending_cash_to_temple(
            db, data["zone_id"], data["id"], data["role"]
        )
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
    if row["role"] not in ("distributor", "zone_admin"):
        raise HTTPException(400, "Selected user is not a distributor or zone leader")
    if not has_role(user, "super_admin") and row["zone_id"] != user["zone_id"]:
        raise HTTPException(403, "Forbidden")

    # Older distributor bills may have no distributor_id, so a distributor's own
    # cashier id identifies them too. A zone leader only counts on bills that
    # explicitly name them as the distributor; their walk-in bills stay zone cash.
    if row["role"] == "distributor":
        owner = "(distributor_id = ? OR (distributor_id IS NULL AND cashier_id = ?))"
        owner_i2 = "(i2.distributor_id = ? OR (i2.distributor_id IS NULL AND i2.cashier_id = ?))"
        owner_params = (user_id, user_id)
    else:
        owner = "distributor_id = ?"
        owner_i2 = "i2.distributor_id = ?"
        owner_params = (user_id,)

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
                             WHERE {owner_i2}
                               AND i2.status IN {SETTLED_SQL}),0) AS books_sold,
                  MAX(created_at) AS last_sale
           FROM invoices
           WHERE {owner}
             AND status IN {SETTLED_SQL}""",
        owner_params + owner_params,
    ).fetchone()

    invoices = db.execute(
        f"""SELECT i.id, i.invoice_number, i.created_at, i.customer_name, i.customer_phone,
                  CASE WHEN i.razorpay_qr_code_id IS NOT NULL THEN 'razorpay'
                       ELSE i.payment_mode END AS payment_mode,
                  i.payment_ref, i.status, i.subtotal, i.discount, i.total,
                  COALESCE((SELECT SUM(p.amount) FROM invoice_payments p
                             WHERE p.invoice_id = i.id),0) AS amount_paid,
                  l.name AS location_name,
                  (SELECT COUNT(*) FROM invoice_items ii WHERE ii.invoice_id = i.id) AS line_count,
                  (SELECT COALESCE(SUM(ii.qty),0) FROM invoice_items ii WHERE ii.invoice_id = i.id) AS books_qty
           FROM invoices i JOIN locations l ON l.id = i.location_id
           WHERE {owner}
           ORDER BY i.id DESC""",
        owner_params,
    ).fetchall()

    books = db.execute(
        f"""SELECT b.id AS book_id, b.title, b.barcode,
                  COALESCE(SUM(ii.qty),0) AS qty,
                  COALESCE(SUM(ii.line_total),0) AS revenue
           FROM invoice_items ii
           JOIN invoices i ON i.id = ii.invoice_id
           JOIN books b ON b.id = ii.book_id
           WHERE {owner}
             AND i.status IN {SETTLED_SQL}
           GROUP BY b.id ORDER BY revenue DESC""",
        owner_params,
    ).fetchall()

    summary_data = dict(summary)
    summary_data["outstanding"] = max(
        0,
        round(float(summary_data["revenue"] or 0) - float(summary_data["collected"] or 0), 2),
    )
    summary_data["pending_to_temple"] = _pending_cash_to_temple(
        db, row["zone_id"], user_id, row["role"]
    )
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
