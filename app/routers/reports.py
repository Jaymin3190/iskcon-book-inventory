from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..auth import has_role, require_roles
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
    if has_role(user, "zone_admin"):
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
        where.append("date(a.created_at) >= date(?)")
        params.append(frm)
    if to:
        where.append("date(a.created_at) <= date(?)")
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
