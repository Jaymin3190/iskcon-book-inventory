from datetime import datetime
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_roles
from ..database import get_db
from ..services.audit import log_audit
from ..services.ledger import check_available, ensure_stock, move_stock
from ..services.scope import can_view, location_ids

router = APIRouter(prefix="/api/stock", tags=["stock"])

DAMAGED_REASON_CODES = ("DAMAGED", "STAINED", "MISSING_PAGES", "PRINT_ERROR", "OTHER")


@router.get("")
def current_stock(location_id: int = None, book_id: int = None, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    ids = location_ids(db, user)
    sql = """SELECT s.book_id, s.location_id, s.qty, s.updated_at,
                    b.title, b.barcode, b.isbn, b.price, b.publication,
                    l.code AS location_code, l.name AS location_name
             FROM stock s
             JOIN books b ON b.id = s.book_id
             JOIN locations l ON l.id = s.location_id
             WHERE s.location_id IN ({})""".format(",".join("?" * len(ids)))
    params = list(ids)
    if location_id:
        sql += " AND s.location_id = ?"
        params.append(location_id)
    if book_id:
        sql += " AND s.book_id = ?"
        params.append(book_id)
    sql += " ORDER BY l.name, b.title"
    return [dict(r) for r in db.execute(sql, params).fetchall()]


@router.get("/ledger")
def stock_ledger(location_id: int = None, book_id: int = None, limit: int = 200, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    ids = location_ids(db, user)
    sql = """SELECT m.*, b.title, l.code AS location_code, u.username AS user_name
             FROM stock_movements m
             JOIN books b ON b.id = m.book_id
             JOIN locations l ON l.id = m.location_id
             LEFT JOIN users u ON u.id = m.user_id
             WHERE m.location_id IN ({})""".format(",".join("?" * len(ids)))
    params = list(ids)
    if location_id:
        sql += " AND m.location_id = ?"
        params.append(location_id)
    if book_id:
        sql += " AND m.book_id = ?"
        params.append(book_id)
    sql += " ORDER BY m.id DESC LIMIT ?"
    params.append(min(limit, 1000))
    return [dict(r) for r in db.execute(sql, params).fetchall()]


class StockAdjustIn(BaseModel):
    book_id: int
    location_id: int
    qty: int
    note: Optional[str] = None


@router.post("/adjust")
def adjust_stock(data: StockAdjustIn, db=Depends(get_db), user=Depends(require_roles("super_admin", "temple"))):
    if data.qty == 0:
        raise HTTPException(400, "Quantity must not be zero")
    if data.location_id not in location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    location = db.execute(
        "SELECT type, parent_id FROM locations WHERE id = ?",
        (data.location_id,),
    ).fetchone()
    if not location or location["type"] != "warehouse" or location["parent_id"] is not None:
        raise HTTPException(403, "Manual stock adjustments are only allowed at the warehouse; use a transfer to assign stock to a zone")
    if user["role"] == "temple" and user["location_id"] != data.location_id:
        raise HTTPException(403, "You can only adjust stock at your assigned warehouse")
    book = db.execute("SELECT * FROM books WHERE id = ?", (data.book_id,)).fetchone()
    if not book:
        raise HTTPException(404, "Book not found")
    change_type = "STOCK_IN" if data.qty > 0 else "STOCK_OUT"
    try:
        move_stock(db, data.book_id, data.location_id, data.qty, change_type, "manual", None, user["id"], data.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    new_qty = ensure_stock(db, data.book_id, data.location_id)
    log_audit(db, user["id"], "STOCK_ADJUST", "stock", None, f"{book['title']} {data.qty:+d} at location {data.location_id} (now {new_qty})", user["username"])
    return {"ok": True, "book_id": data.book_id, "location_id": data.location_id, "qty": new_qty}


class CountItemIn(BaseModel):
    book_id: int
    counted_qty: int


class CountStartIn(BaseModel):
    location_id: int


@router.post("/counts")
def create_count(data: CountStartIn, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    location_id = data.location_id
    if location_id not in location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    cur = db.execute(
        "INSERT INTO stock_counts (location_id, status, counted_by) VALUES (?, 'open', ?)",
        (location_id, user["id"]),
    )
    db.commit()
    log_audit(db, user["id"], "STOCK_COUNT_START", "stock_counts", cur.lastrowid, f"location {location_id}", user["username"])
    return {"count_id": cur.lastrowid, "location_id": location_id}


@router.post("/counts/{count_id}/items")
def add_count_items(count_id: int, items: list[CountItemIn], db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    count = db.execute("SELECT * FROM stock_counts WHERE id = ?", (count_id,)).fetchone()
    if not count:
        raise HTTPException(404, "Count not found")
    if count["status"] != "open":
        raise HTTPException(400, "Count is already completed")
    if count["location_id"] not in location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    for it in items:
        db.execute(
            """INSERT INTO stock_count_items (count_id, book_id, counted_qty)
               VALUES (?,?,?)
               ON CONFLICT(count_id, book_id) DO UPDATE SET counted_qty = excluded.counted_qty""",
            (count_id, it.book_id, it.counted_qty),
        )
    db.commit()
    return {"ok": True, "items": len(items)}


@router.post("/counts/{count_id}/complete")
def complete_count(count_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    count = db.execute("SELECT * FROM stock_counts WHERE id = ?", (count_id,)).fetchone()
    if not count:
        raise HTTPException(404, "Count not found")
    if count["status"] != "open":
        raise HTTPException(400, "Count is already completed")
    if count["location_id"] not in location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    items = db.execute("SELECT * FROM stock_count_items WHERE count_id = ?", (count_id,)).fetchall()
    if not items:
        raise HTTPException(400, "No items counted")
    variances = []
    for it in items:
        system_qty = ensure_stock(db, it["book_id"], count["location_id"])
        variance = it["counted_qty"] - system_qty
        db.execute(
            "UPDATE stock_count_items SET system_qty = ?, variance = ? WHERE id = ?",
            (system_qty, variance, it["id"]),
        )
        if variance != 0:
            move_stock(db, it["book_id"], count["location_id"], variance, "ADJUST", "stock_counts", count_id, user["id"], f"stock count adjustment (count {count_id})")
        variances.append(variance)
    db.execute(
        "UPDATE stock_counts SET status = 'completed', completed_at = datetime('now') WHERE id = ?",
        (count_id,),
    )
    db.commit()
    log_audit(db, user["id"], "STOCK_COUNT_COMPLETE", "stock_counts", count_id, f"location {count['location_id']}, variances {variances}", user["username"])
    return {"ok": True, "variances": variances}


@router.delete("/counts/{count_id}")
def delete_count(count_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    count = db.execute("SELECT * FROM stock_counts WHERE id = ?", (count_id,)).fetchone()
    if not count:
        raise HTTPException(404, "Count not found")
    if count["status"] != "open":
        raise HTTPException(409, "Cannot delete: count already completed (adjustments applied)")
    if count["location_id"] not in location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    db.execute("DELETE FROM stock_count_items WHERE count_id = ?", (count_id,))
    db.execute("DELETE FROM stock_counts WHERE id = ?", (count_id,))
    db.commit()
    log_audit(db, user["id"], "STOCK_COUNT_DELETE", "stock_counts", count_id, f"location {count['location_id']}", user["username"])
    return {"ok": True}


class TransferItemIn(BaseModel):
    book_id: int
    qty: int


class TransferIn(BaseModel):
    from_location_id: int
    to_location_id: int
    note: Optional[str] = None
    items: list[TransferItemIn]


@router.get("/transfers")
def list_transfers(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    ids = location_ids(db, user)
    sql = """SELECT t.*,
                    f.code AS from_code, f.name AS from_name,
                    to_l.code AS to_code, to_l.name AS to_name,
                    ru.username AS requested_by_name
             FROM transfers t
             JOIN locations f ON f.id = t.from_location_id
             JOIN locations to_l ON to_l.id = t.to_location_id
             LEFT JOIN users ru ON ru.id = t.requested_by
             WHERE t.from_location_id IN ({}) OR t.to_location_id IN ({})
             ORDER BY t.id DESC""".format(",".join("?" * len(ids)), ",".join("?" * len(ids)))
    params = list(ids) + list(ids)
    return [dict(r) for r in db.execute(sql, params).fetchall()]


@router.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    t = db.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
    if not t:
        raise HTTPException(404, "Transfer not found")
    if not can_view(db, user, t["from_location_id"]) and not can_view(db, user, t["to_location_id"]):
        raise HTTPException(403, "Forbidden")
    items = db.execute(
        """SELECT ti.*, b.title, b.barcode, b.price
           FROM transfer_items ti JOIN books b ON b.id = ti.book_id
           WHERE ti.transfer_id = ?""",
        (transfer_id,),
    ).fetchall()
    data = dict(t)
    data["items"] = [dict(r) for r in items]
    return data


@router.delete("/transfers/{transfer_id}")
def delete_transfer(transfer_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    t = db.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
    if not t:
        raise HTTPException(404, "Transfer not found")
    if t["status"] not in ("requested", "rejected"):
        raise HTTPException(409, "Cannot delete: transfer is already in progress or completed")
    if not can_view(db, user, t["from_location_id"]) and not can_view(db, user, t["to_location_id"]):
        raise HTTPException(403, "Forbidden")
    db.execute("DELETE FROM transfer_items WHERE transfer_id = ?", (transfer_id,))
    db.execute("DELETE FROM transfers WHERE id = ?", (transfer_id,))
    db.commit()
    log_audit(db, user["id"], "TRANSFER_DELETE", "transfers", transfer_id, t["request_number"], user["username"])
    return {"ok": True}


@router.post("/transfers")
def request_transfer(data: TransferIn, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    if not data.items:
        raise HTTPException(400, "At least one item is required")
    if not can_view(db, user, data.from_location_id) or not can_view(db, user, data.to_location_id):
        raise HTTPException(403, "Forbidden")
    source = db.execute(
        "SELECT type, parent_id FROM locations WHERE id = ?",
        (data.from_location_id,),
    ).fetchone()
    destination = db.execute(
        "SELECT type FROM locations WHERE id = ?",
        (data.to_location_id,),
    ).fetchone()
    if not source or source["type"] != "warehouse" or source["parent_id"] is not None:
        raise HTTPException(400, "Stock can only be issued from the root warehouse")
    if not destination or destination["type"] == "warehouse":
        raise HTTPException(400, "Choose a zone or store as the transfer destination")
    for it in data.items:
        if it.qty <= 0:
            raise HTTPException(400, "Quantities must be positive")

    requested_qty = {}
    for it in data.items:
        requested_qty[it.book_id] = requested_qty.get(it.book_id, 0) + it.qty
    shortages = []
    for book_id, qty in requested_qty.items():
        book = db.execute("SELECT title FROM books WHERE id = ?", (book_id,)).fetchone()
        if not book:
            raise HTTPException(404, f"Book not found: {book_id}")
        if not check_available(db, book_id, data.from_location_id, qty):
            shortages.append(f"{book['title']} (need {qty})")
    if shortages:
        raise HTTPException(400, "Insufficient warehouse stock to issue: " + ", ".join(shortages))

    num = f"TRF-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:12].upper()}"
    cur = db.execute(
        """INSERT INTO transfers (request_number, from_location_id, to_location_id, status, requested_by, note)
           VALUES (?,?,?, 'requested', ?, ?)""",
        (num, data.from_location_id, data.to_location_id, user["id"], data.note),
    )
    tid = cur.lastrowid
    for book_id, qty in requested_qty.items():
        db.execute(
            "INSERT INTO transfer_items (transfer_id, book_id, qty) VALUES (?,?,?)",
            (tid, book_id, qty),
        )
    db.commit()
    log_audit(db, user["id"], "TRANSFER_REQUEST", "transfers", tid, num, user["username"])
    return {"id": tid, "request_number": num, "status": "requested"}


@router.post("/transfers/{transfer_id}/approve")
def approve_transfer(transfer_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    t = db.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
    if not t:
        raise HTTPException(404, "Transfer not found")
    if t["status"] != "requested":
        raise HTTPException(400, f"Cannot approve transfer in status '{t['status']}'")
    if user["role"] == "zone_admin" and not (can_view(db, user, t["from_location_id"]) or can_view(db, user, t["to_location_id"])):
        raise HTTPException(403, "Forbidden")
    db.execute(
        "UPDATE transfers SET status = 'approved', approved_by = ?, approved_at = datetime('now') WHERE id = ?",
        (user["id"], transfer_id),
    )
    db.commit()
    log_audit(db, user["id"], "TRANSFER_APPROVE", "transfers", transfer_id, t["request_number"], user["username"])
    return {"ok": True, "status": "approved"}


@router.post("/transfers/{transfer_id}/reject")
def reject_transfer(transfer_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    t = db.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
    if not t:
        raise HTTPException(404, "Transfer not found")
    if t["status"] != "requested":
        raise HTTPException(400, f"Cannot reject transfer in status '{t['status']}'")
    if user["role"] == "zone_admin" and not (can_view(db, user, t["from_location_id"]) or can_view(db, user, t["to_location_id"])):
        raise HTTPException(403, "Forbidden")
    db.execute("UPDATE transfers SET status = 'rejected' WHERE id = ?", (transfer_id,))
    db.commit()
    log_audit(db, user["id"], "TRANSFER_REJECT", "transfers", transfer_id, t["request_number"], user["username"])
    return {"ok": True, "status": "rejected"}


@router.post("/transfers/{transfer_id}/dispatch")
def dispatch_transfer(transfer_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor"))):
    t = db.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
    if not t:
        raise HTTPException(404, "Transfer not found")
    if t["status"] != "approved":
        raise HTTPException(400, f"Cannot dispatch transfer in status '{t['status']}'")
    if user["role"] != "super_admin" and not can_view(db, user, t["from_location_id"]):
        raise HTTPException(403, "Forbidden")
    source = db.execute(
        "SELECT type, parent_id FROM locations WHERE id = ?",
        (t["from_location_id"],),
    ).fetchone()
    if not source or source["type"] != "warehouse" or source["parent_id"] is not None:
        raise HTTPException(400, "Stock can only be issued from the root warehouse")
    items = db.execute("SELECT * FROM transfer_items WHERE transfer_id = ?", (transfer_id,)).fetchall()
    shortages = []
    requested_qty = {}
    for it in items:
        requested_qty[it["book_id"]] = requested_qty.get(it["book_id"], 0) + it["qty"]
    for book_id, qty in requested_qty.items():
        if not check_available(db, book_id, t["from_location_id"], qty):
            book = db.execute("SELECT title FROM books WHERE id = ?", (book_id,)).fetchone()
            shortages.append(f"{book['title']} (need {qty})")
    if shortages:
        raise HTTPException(400, "Insufficient warehouse stock to issue: " + ", ".join(shortages))
    for book_id, qty in requested_qty.items():
        move_stock(db, book_id, t["from_location_id"], -qty, "TRANSFER_OUT", "transfers", transfer_id, user["id"], t["request_number"])
    db.execute(
        "UPDATE transfers SET status = 'dispatched', dispatched_by = ?, dispatched_at = datetime('now') WHERE id = ?",
        (user["id"], transfer_id),
    )
    db.commit()
    log_audit(db, user["id"], "TRANSFER_DISPATCH", "transfers", transfer_id, t["request_number"], user["username"])
    return {"ok": True, "status": "dispatched"}


@router.post("/transfers/{transfer_id}/receive")
def receive_transfer(transfer_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    t = db.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
    if not t:
        raise HTTPException(404, "Transfer not found")
    if t["status"] != "dispatched":
        raise HTTPException(400, f"Cannot receive transfer in status '{t['status']}'")
    if user["role"] not in ("super_admin",) and not (
        (user["role"] == "temple" and user["location_id"] == t["to_location_id"])
        or (user["role"] in ("zone_admin", "distributor") and can_view(db, user, t["to_location_id"]))
    ):
        raise HTTPException(403, "Forbidden")
    items = db.execute("SELECT * FROM transfer_items WHERE transfer_id = ?", (transfer_id,)).fetchall()
    for it in items:
        move_stock(db, it["book_id"], t["to_location_id"], it["qty"], "TRANSFER_IN", "transfers", transfer_id, user["id"], t["request_number"])
    db.execute(
        "UPDATE transfers SET status = 'received', received_by = ?, received_at = datetime('now') WHERE id = ?",
        (user["id"], transfer_id),
    )
    db.commit()
    log_audit(db, user["id"], "TRANSFER_RECEIVE", "transfers", transfer_id, t["request_number"], user["username"])
    return {"ok": True, "status": "received"}


class ReturnItemIn(BaseModel):
    book_id: int
    qty: int


class ReturnIn(BaseModel):
    location_id: int
    reason: Optional[str] = None
    items: list[ReturnItemIn]


@router.get("/returns")
def list_returns(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    ids = location_ids(db, user)
    sql = """SELECT r.*, l.code AS location_code, l.name AS location_name,
                    (SELECT COUNT(*) FROM return_items WHERE return_id = r.id) AS item_count,
                    (SELECT COALESCE(SUM(qty),0) FROM return_items WHERE return_id = r.id) AS units
             FROM returns r
             JOIN locations l ON l.id = r.location_id
             WHERE r.location_id IN ({})
             ORDER BY r.id DESC""".format(",".join("?" * len(ids)))
    return [dict(r) for r in db.execute(sql, list(ids)).fetchall()]


@router.post("/returns")
def create_return(data: ReturnIn, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    if not data.items:
        raise HTTPException(400, "At least one item is required")
    if data.location_id not in location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    shortages = []
    for it in data.items:
        if not check_available(db, it.book_id, data.location_id, it.qty):
            book = db.execute("SELECT title FROM books WHERE id = ?", (it.book_id,)).fetchone()
            shortages.append(f"{book['title']} (need {it.qty})")
    if shortages:
        raise HTTPException(400, "Insufficient stock to return: " + ", ".join(shortages))
    num = f"RET-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:12].upper()}"
    cur = db.execute(
        "INSERT INTO returns (return_number, location_id, reason, created_by) VALUES (?,?,?,?)",
        (num, data.location_id, data.reason, user["id"]),
    )
    rid = cur.lastrowid
    for it in data.items:
        db.execute(
            "INSERT INTO return_items (return_id, book_id, qty) VALUES (?,?,?)",
            (rid, it.book_id, it.qty),
        )
        move_stock(db, it.book_id, data.location_id, -it.qty, "RETURN_OUT", "returns", rid, user["id"], num)
    db.commit()
    log_audit(db, user["id"], "RETURN_CREATE", "returns", rid, num, user["username"])
    return {"id": rid, "return_number": num, "status": "recorded"}


@router.patch("/returns/{return_id}/process")
def process_return(return_id: int, credit_location_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    r = db.execute("SELECT * FROM returns WHERE id = ?", (return_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Return not found")
    if r["status"] != "recorded":
        raise HTTPException(400, "Return already processed")
    if credit_location_id not in location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    items = db.execute("SELECT * FROM return_items WHERE return_id = ?", (return_id,)).fetchall()
    for it in items:
        move_stock(db, it["book_id"], credit_location_id, it["qty"], "RETURN_IN", "returns", return_id, user["id"], r["return_number"])
    db.execute(
        "UPDATE returns SET status = 'processed', credit_location_id = ?, processed_by = ?, processed_at = datetime('now') WHERE id = ?",
        (credit_location_id, user["id"], return_id),
    )
    db.commit()
    log_audit(db, user["id"], "RETURN_PROCESS", "returns", return_id, r["return_number"], user["username"])
    return {"ok": True, "status": "processed"}


@router.delete("/returns/{return_id}")
def delete_return(return_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    r = db.execute("SELECT * FROM returns WHERE id = ?", (return_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Return not found")
    if r["status"] != "recorded":
        raise HTTPException(409, "Cannot delete: return already processed")
    if r["location_id"] not in location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    items = db.execute("SELECT * FROM return_items WHERE return_id = ?", (return_id,)).fetchall()
    for item in items:
        move_stock(
            db,
            item["book_id"],
            r["location_id"],
            item["qty"],
            "RETURN_REVERSED",
            "returns",
            return_id,
            user["id"],
            "recorded return deleted",
        )
    db.execute("DELETE FROM return_items WHERE return_id = ?", (return_id,))
    db.execute("DELETE FROM returns WHERE id = ?", (return_id,))
    db.commit()
    log_audit(db, user["id"], "RETURN_DELETE", "returns", return_id, r["return_number"], user["username"])
    return {"ok": True}


class DamagedIn(BaseModel):
    location_id: int
    book_id: int
    qty: int
    reason_code: str
    note: Optional[str] = None


@router.get("/damaged")
def list_damaged(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    ids = location_ids(db, user)
    sql = """SELECT d.*, l.code AS location_code, b.title
             FROM damaged d
             JOIN locations l ON l.id = d.location_id
             JOIN books b ON b.id = d.book_id
             WHERE d.location_id IN ({})
             ORDER BY d.id DESC""".format(",".join("?" * len(ids)))
    return [dict(r) for r in db.execute(sql, list(ids)).fetchall()]


@router.post("/damaged")
def record_damaged(data: DamagedIn, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    if data.reason_code not in DAMAGED_REASON_CODES:
        raise HTTPException(400, f"Invalid reason code. Valid: {', '.join(DAMAGED_REASON_CODES)}")
    if data.qty <= 0:
        raise HTTPException(400, "Quantity must be positive")
    if data.location_id not in location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    if not check_available(db, data.book_id, data.location_id, data.qty):
        raise HTTPException(400, "Insufficient stock to write off")
    cur = db.execute(
        "INSERT INTO damaged (location_id, book_id, qty, reason_code, note, created_by) VALUES (?,?,?,?,?,?)",
        (data.location_id, data.book_id, data.qty, data.reason_code, data.note, user["id"]),
    )
    move_stock(db, data.book_id, data.location_id, -data.qty, "DAMAGED_OUT", "damaged", cur.lastrowid, user["id"], data.reason_code)
    db.commit()
    log_audit(db, user["id"], "DAMAGED_WRITEOFF", "damaged", cur.lastrowid, f"{data.reason_code} qty {data.qty}", user["username"])
    return {"id": cur.lastrowid, "ok": True}


@router.delete("/damaged/{damaged_id}")
def delete_damaged(damaged_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    d = db.execute("SELECT * FROM damaged WHERE id = ?", (damaged_id,)).fetchone()
    if not d:
        raise HTTPException(404, "Damaged record not found")
    if d["location_id"] not in location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    move_stock(db, d["book_id"], d["location_id"], d["qty"], "DAMAGED_REVERSED", "damaged", damaged_id, user["id"], "write-off deleted")
    db.execute("DELETE FROM damaged WHERE id = ?", (damaged_id,))
    db.commit()
    log_audit(db, user["id"], "DAMAGED_DELETE", "damaged", damaged_id, f"restock qty {d['qty']}", user["username"])
    return {"ok": True, "restocked_qty": d["qty"]}
