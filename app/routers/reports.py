from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import has_role, require_roles
from ..database import get_db
from ..services.scope import invoice_location_ids, location_ids, stock_location_ids, zone_location_ids

router = APIRouter(tags=["reports"])


@router.get("/api/reports/sales")
def sales_report(frm: str = None, to: str = None, location_id: int = None, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    ids = invoice_location_ids(db, user)
    sql = """SELECT i.location_id, l.code AS location_code, l.name AS location_name,
                    COUNT(*) AS invoice_count, COALESCE(SUM(i.total),0) AS revenue,
                    COALESCE(SUM(i.subtotal),0) AS subtotal, COALESCE(SUM(i.discount),0) AS discount
             FROM invoices i
             JOIN locations l ON l.id = i.location_id
             WHERE i.location_id IN ({}) AND i.status = 'paid'""".format(",".join("?" * len(ids)))
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
    return [dict(r) for r in db.execute(sql, params).fetchall()]


@router.get("/api/reports/sales-by-book")
def sales_by_book(frm: str = None, to: str = None, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    ids = invoice_location_ids(db, user)
    sql = """SELECT b.id AS book_id, b.title, b.barcode,
                    COALESCE(SUM(ii.qty),0) AS qty_sold, COALESCE(SUM(ii.line_total),0) AS revenue
             FROM invoice_items ii
             JOIN invoices i ON i.id = ii.invoice_id
             JOIN books b ON b.id = ii.book_id
             WHERE i.location_id IN ({}) AND i.status = 'paid'""".format(",".join("?" * len(ids)))
    params = list(ids)
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
             JOIN users u ON u.id = i.cashier_id AND u.role = 'distributor'
             JOIN locations l ON l.id = i.location_id
             JOIN invoice_items ii ON ii.invoice_id = i.id
             JOIN books b ON b.id = ii.book_id
             WHERE i.location_id IN ({}) AND i.status = 'paid'""".format(",".join("?" * len(ids)))
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
                          COALESCE(SUM(CASE WHEN i.payment_mode = 'cash' THEN i.total ELSE 0 END),0) AS cash_total,
                          COALESCE(SUM(CASE WHEN i.payment_mode = 'upi' AND i.razorpay_qr_code_id IS NULL THEN i.total ELSE 0 END),0) AS upi_total,
                          COALESCE(SUM(CASE WHEN i.razorpay_qr_code_id IS NOT NULL THEN i.total ELSE 0 END),0) AS razorpay_total
                     FROM invoices i JOIN tree t ON t.id = i.location_id
                    WHERE i.status = 'paid'
                    GROUP BY t.root_id
               ),
               sold_agg AS (
                   SELECT t.root_id, COALESCE(SUM(ii.qty),0) AS books_sold
                     FROM invoices i
                     JOIN tree t ON t.id = i.location_id
                     JOIN invoice_items ii ON ii.invoice_id = i.id
                    WHERE i.status = 'paid'
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
                    COALESCE(ia.cash_total,0) AS cash_total,
                    COALESCE(ia.upi_total,0) AS upi_total,
                    COALESCE(ia.razorpay_total,0) AS razorpay_total,
                    COALESCE(sa.books_sold,0) AS books_sold
             FROM locations l
             LEFT JOIN invoice_agg ia ON ia.root_id = l.id
             LEFT JOIN sold_agg sa ON sa.root_id = l.id
             WHERE l.type = 'zone' AND l.id IN ({})
             ORDER BY l.name""".format(",".join("?" * len(ids)))
    return [dict(r) for r in db.execute(sql, list(ids)).fetchall()]


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
                    i.payment_ref, i.status, i.total,
                    l.name AS location_name,
                    u.name AS cashier_name, u.username AS cashier_username,
                    (SELECT COALESCE(SUM(ii.qty),0) FROM invoice_items ii WHERE ii.invoice_id = i.id) AS books_qty
             FROM invoices i
             JOIN locations l ON l.id = i.location_id
             LEFT JOIN users u ON u.id = i.cashier_id
             WHERE i.location_id IN ({})
             ORDER BY i.id DESC""".format(",".join("?" * len(ids)))
    return [dict(r) for r in db.execute(sql, list(ids)).fetchall()]


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
                     (SELECT COUNT(*) FROM invoices i WHERE i.cashier_id = u.id AND i.status = 'paid') AS invoice_count,
                     (SELECT COALESCE(SUM(i.total),0) FROM invoices i WHERE i.cashier_id = u.id AND i.status = 'paid') AS revenue,
                     (SELECT COALESCE(SUM(i.total),0) FROM invoices i
                       WHERE i.cashier_id = u.id AND i.payment_mode = 'cash' AND i.status = 'paid') AS cash_total,
                     (SELECT COALESCE(SUM(i.total),0) FROM invoices i
                       WHERE i.cashier_id = u.id AND i.payment_mode = 'upi'
                         AND i.razorpay_qr_code_id IS NULL AND i.status = 'paid') AS upi_total,
                     (SELECT COALESCE(SUM(i.total),0) FROM invoices i
                       WHERE i.cashier_id = u.id AND i.razorpay_qr_code_id IS NOT NULL
                         AND i.status = 'paid') AS razorpay_total,
                     (SELECT COALESCE(SUM(ii.qty),0)
                        FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id
                       WHERE i.cashier_id = u.id AND i.status = 'paid') AS books_sold,
                     (SELECT MAX(i.created_at) FROM invoices i WHERE i.cashier_id = u.id AND i.status = 'paid') AS last_sale
              FROM users u
              JOIN locations l ON l.id = u.zone_id
              WHERE {where}
              ORDER BY l.name, u.name"""
    return [dict(r) for r in db.execute(sql, params).fetchall()]


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
        """SELECT COUNT(*) AS invoice_count,
                  COALESCE(SUM(total),0) AS revenue,
                  COALESCE(SUM(subtotal),0) AS subtotal,
                  COALESCE(SUM(discount),0) AS discount,
                  COALESCE(SUM(CASE WHEN payment_mode = 'cash' THEN total ELSE 0 END),0) AS cash_total,
                  COALESCE(SUM(CASE WHEN payment_mode = 'upi' AND razorpay_qr_code_id IS NULL THEN total ELSE 0 END),0) AS upi_total,
                  COALESCE(SUM(CASE WHEN razorpay_qr_code_id IS NOT NULL THEN total ELSE 0 END),0) AS razorpay_total,
                  COALESCE((SELECT SUM(ii.qty) FROM invoice_items ii
                              JOIN invoices i2 ON i2.id = ii.invoice_id
                             WHERE i2.cashier_id = ? AND i2.status = 'paid'),0) AS books_sold,
                  MAX(created_at) AS last_sale
           FROM invoices WHERE cashier_id = ? AND status = 'paid'""",
        (user_id, user_id),
    ).fetchone()

    invoices = db.execute(
        """SELECT i.id, i.invoice_number, i.created_at, i.customer_name, i.customer_phone,
                  CASE WHEN i.razorpay_qr_code_id IS NOT NULL THEN 'razorpay'
                       ELSE i.payment_mode END AS payment_mode,
                  i.payment_ref, i.status, i.subtotal, i.discount, i.total,
                  l.name AS location_name,
                  (SELECT COUNT(*) FROM invoice_items ii WHERE ii.invoice_id = i.id) AS line_count,
                  (SELECT COALESCE(SUM(ii.qty),0) FROM invoice_items ii WHERE ii.invoice_id = i.id) AS books_qty
           FROM invoices i JOIN locations l ON l.id = i.location_id
           WHERE i.cashier_id = ?
           ORDER BY i.id DESC""",
        (user_id,),
    ).fetchall()

    books = db.execute(
        """SELECT b.id AS book_id, b.title, b.barcode,
                  COALESCE(SUM(ii.qty),0) AS qty,
                  COALESCE(SUM(ii.line_total),0) AS revenue
           FROM invoice_items ii
           JOIN invoices i ON i.id = ii.invoice_id
           JOIN books b ON b.id = ii.book_id
           WHERE i.cashier_id = ? AND i.status = 'paid'
           GROUP BY b.id ORDER BY revenue DESC""",
        (user_id,),
    ).fetchall()

    return {
        "distributor": dict(row),
        "summary": dict(summary),
        "invoices": [dict(r) for r in invoices],
        "books": [dict(r) for r in books],
    }


@router.get("/api/reports/transfers")
def transfers_report(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    ids = location_ids(db, user)
    sql = """SELECT status, COUNT(*) AS n
             FROM transfers
             WHERE from_location_id IN ({}) OR to_location_id IN ({})
             GROUP BY status""".format(",".join("?" * len(ids)), ",".join("?" * len(ids)))
    params = list(ids) + list(ids)
    return [dict(r) for r in db.execute(sql, params).fetchall()]


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
