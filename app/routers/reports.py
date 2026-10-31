from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..auth import require_roles
from ..database import get_db
from ..services.scope import location_ids

router = APIRouter(tags=["reports"])


@router.get("/api/reports/sales")
def sales_report(frm: str = None, to: str = None, location_id: int = None, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    ids = location_ids(db, user)
    sql = """SELECT i.location_id, l.code AS location_code, l.name AS location_name,
                    COUNT(*) AS invoice_count, COALESCE(SUM(i.total),0) AS revenue,
                    COALESCE(SUM(i.subtotal),0) AS subtotal, COALESCE(SUM(i.discount),0) AS discount
             FROM invoices i
             JOIN locations l ON l.id = i.location_id
             WHERE i.location_id IN ({})""".format(",".join("?" * len(ids)))
    params = list(ids)
    if frm:
        sql += " AND i.created_at >= ?"
        params.append(frm)
    if to:
        sql += " AND i.created_at <= ?"
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
    ids = location_ids(db, user)
    sql = """SELECT b.id AS book_id, b.title, b.barcode,
                    COALESCE(SUM(ii.qty),0) AS qty_sold, COALESCE(SUM(ii.line_total),0) AS revenue
             FROM invoice_items ii
             JOIN invoices i ON i.id = ii.invoice_id
             JOIN books b ON b.id = ii.book_id
             WHERE i.location_id IN ({})""".format(",".join("?" * len(ids)))
    params = list(ids)
    if frm:
        sql += " AND i.created_at >= ?"
        params.append(frm)
    if to:
        sql += " AND i.created_at <= ?"
        params.append(to + " 23:59:59")
    sql += " GROUP BY b.id ORDER BY revenue DESC"
    return [dict(r) for r in db.execute(sql, params).fetchall()]


@router.get("/api/reports/distributor-sales")
def distributor_sales_report(frm: str = None, to: str = None, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    ids = location_ids(db, user)
    sql = """SELECT i.created_at AS sold_at, i.invoice_number,
                    u.name AS distributor_name, u.username AS distributor_username,
                    l.name AS location_name, b.title AS book_title, b.barcode,
                    ii.qty, i.payment_mode, ii.line_total AS amount
             FROM invoices i
             JOIN users u ON u.id = i.cashier_id AND u.role = 'distributor'
             JOIN locations l ON l.id = i.location_id
             JOIN invoice_items ii ON ii.invoice_id = i.id
             JOIN books b ON b.id = ii.book_id
             WHERE i.location_id IN ({})""".format(",".join("?" * len(ids)))
    params = list(ids)
    if user["role"] == "zone_admin":
        sql += " AND u.zone_id = ?"
        params.append(user["zone_id"])
    if frm:
        sql += " AND i.created_at >= ?"
        params.append(frm)
    if to:
        sql += " AND i.created_at <= ?"
        params.append(to + " 23:59:59")
    sql += " ORDER BY i.created_at DESC, i.id DESC, ii.id"
    return [dict(r) for r in db.execute(sql, params).fetchall()]


@router.get("/api/reports/stock-summary")
def stock_summary(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    ids = location_ids(db, user)
    sql = """SELECT l.id AS location_id, l.code, l.name, l.type, l.parent_id,
                    COUNT(s.book_id) AS skus, COALESCE(SUM(s.qty),0) AS total_units,
                    COALESCE(SUM(s.qty * b.price),0) AS stock_value
             FROM locations l
             LEFT JOIN stock s ON s.location_id = l.id
             LEFT JOIN books b ON b.id = s.book_id
             WHERE l.id IN ({})
             GROUP BY l.id ORDER BY l.type, l.name""".format(",".join("?" * len(ids)))
    return [dict(r) for r in db.execute(sql, list(ids)).fetchall()]


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


@router.get("/api/audit")
def audit_trail(limit: int = 100, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    if user["role"] == "zone_admin":
        zone_ids = location_ids(db, user)
        return [dict(r) for r in db.execute(
            """SELECT a.* FROM audit_logs a
               WHERE a.action IN ('LOGIN') OR a.username = ? OR a.user_id = ?
               ORDER BY a.id DESC LIMIT ?""",
            (user["username"], user["id"], min(limit, 500)),
        ).fetchall()]
    return [dict(r) for r in db.execute(
        "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?",
        (min(limit, 500),),
    ).fetchall()]
