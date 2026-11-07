from typing import Optional
from uuid import uuid4

from html import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..auth import require_roles
from ..database import get_db
from ..services.audit import log_audit
from ..services.ledger import check_available, ensure_stock, move_stock
from ..services.scope import can_view, location_ids, zone_location_ids, stock_location_ids
from ..services.timezone import format_ist_datetime, now_ist

router = APIRouter(prefix="/api/stock", tags=["stock"])

DAMAGED_REASON_CODES = ("DAMAGED", "STAINED", "MISSING_PAGES", "PRINT_ERROR", "OTHER")


def _can_view_transfer(db, user, transfer) -> bool:
    if user["role"] == "zone_admin":
        return transfer["to_location_id"] in zone_location_ids(db, user["zone_id"])
    return (
        can_view(db, user, transfer["from_location_id"])
        or can_view(db, user, transfer["to_location_id"])
    )


@router.get("")
def current_stock(location_id: int = None, book_id: int = None, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    ids = stock_location_ids(db, user)
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
def stock_ledger(location_id: int = None, book_id: int = None, limit: int = 200, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    ids = stock_location_ids(db, user)
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


class StockAdjustItemIn(BaseModel):
    book_id: int
    qty: int


class StockAdjustBatchIn(BaseModel):
    location_id: int
    items: list[StockAdjustItemIn]
    note: Optional[str] = None


@router.post("/adjust/batch")
def adjust_stock_batch(data: StockAdjustBatchIn, db=Depends(get_db), user=Depends(require_roles("super_admin", "temple"))):
    if not data.items:
        raise HTTPException(400, "Add at least one book")
    if any(item.qty == 0 for item in data.items):
        raise HTTPException(400, "Quantity must not be zero")
    book_ids = [item.book_id for item in data.items]
    if len(set(book_ids)) != len(book_ids):
        raise HTTPException(400, "A book can only be listed once")
    if data.location_id not in location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    location = db.execute(
        "SELECT type, parent_id FROM locations WHERE id = ?",
        (data.location_id,),
    ).fetchone()
    if not location or location["type"] != "warehouse" or location["parent_id"] is not None:
        raise HTTPException(403, "Manual stock adjustments are only allowed at the warehouse; use a transfer to assign stock to a zone")
    placeholders = ",".join("?" * len(book_ids))
    books = db.execute(
        f"SELECT id, title FROM books WHERE id IN ({placeholders})",
        book_ids,
    ).fetchall()
    books_by_id = {book["id"]: book for book in books}
    missing_ids = [book_id for book_id in book_ids if book_id not in books_by_id]
    if missing_ids:
        raise HTTPException(404, "Book not found: " + ", ".join(map(str, missing_ids)))

    updated = []
    try:
        for item in data.items:
            move_stock(
                db, item.book_id, data.location_id, item.qty,
                "STOCK_IN" if item.qty > 0 else "STOCK_OUT",
                "manual", None, user["id"], data.note, commit=False,
            )
            updated.append({
                "book_id": item.book_id,
                "qty": ensure_stock(db, item.book_id, data.location_id),
            })
        details = "; ".join(
            f"{books_by_id[item.book_id]['title']} {item.qty:+d} (now {new['qty']})"
            for item, new in zip(data.items, updated)
        )
        log_audit(
            db, user["id"], "STOCK_ADJUST", "stock", None,
            f"{details} at location {data.location_id}" +
            (f" - {data.note.strip()}" if data.note and data.note.strip() else ""),
            user["username"],
        )
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return {"ok": True, "location_id": data.location_id, "items": updated}


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
    if location_id not in stock_location_ids(db, user):
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
    if count["location_id"] not in stock_location_ids(db, user):
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
    if count["location_id"] not in stock_location_ids(db, user):
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
    if count["location_id"] not in stock_location_ids(db, user):
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
def list_transfers(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    if user["role"] == "zone_admin":
        ids = zone_location_ids(db, user["zone_id"])
        sql = """SELECT t.*,
                        f.code AS from_code, f.name AS from_name,
                        to_l.code AS to_code, to_l.name AS to_name,
                        ru.username AS requested_by_name,
                        (SELECT COUNT(*) FROM transfer_items ti WHERE ti.transfer_id = t.id) AS item_count,
                        (SELECT COALESCE(SUM(ti.qty),0) FROM transfer_items ti WHERE ti.transfer_id = t.id) AS units
                 FROM transfers t
                 JOIN locations f ON f.id = t.from_location_id
                 JOIN locations to_l ON to_l.id = t.to_location_id
                 LEFT JOIN users ru ON ru.id = t.requested_by
                 WHERE t.to_location_id IN ({})
                 ORDER BY t.id DESC""".format(",".join("?" * len(ids)))
        return [
            dict(r) for r in db.execute(sql, list(ids)).fetchall()
        ]
    ids = location_ids(db, user)
    sql = """SELECT t.*,
                    f.code AS from_code, f.name AS from_name,
                    to_l.code AS to_code, to_l.name AS to_name,
                    ru.username AS requested_by_name,
                    (SELECT COUNT(*) FROM transfer_items ti WHERE ti.transfer_id = t.id) AS item_count,
                    (SELECT COALESCE(SUM(ti.qty),0) FROM transfer_items ti WHERE ti.transfer_id = t.id) AS units
             FROM transfers t
             JOIN locations f ON f.id = t.from_location_id
             JOIN locations to_l ON to_l.id = t.to_location_id
             LEFT JOIN users ru ON ru.id = t.requested_by
             WHERE t.from_location_id IN ({}) OR t.to_location_id IN ({})
             ORDER BY t.id DESC""".format(",".join("?" * len(ids)), ",".join("?" * len(ids)))
    params = list(ids) + list(ids)
    return [dict(r) for r in db.execute(sql, params).fetchall()]


def _load_transfer(db, transfer_id: int):
    t = db.execute(
        """SELECT t.*, f.code AS from_code, f.name AS from_name,
                  to_l.code AS to_code, to_l.name AS to_name,
                  ru.username AS requested_by_name,
                  au.username AS approved_by_name,
                  du.username AS dispatched_by_name,
                  rcu.username AS received_by_name
           FROM transfers t
           JOIN locations f ON f.id = t.from_location_id
           JOIN locations to_l ON to_l.id = t.to_location_id
           LEFT JOIN users ru ON ru.id = t.requested_by
           LEFT JOIN users au ON au.id = t.approved_by
           LEFT JOIN users du ON du.id = t.dispatched_by
           LEFT JOIN users rcu ON rcu.id = t.received_by
           WHERE t.id = ?""",
        (transfer_id,),
    ).fetchone()
    if not t:
        raise HTTPException(404, "Transfer not found")
    items = db.execute(
        """SELECT ti.*, b.title, b.barcode, b.price
           FROM transfer_items ti JOIN books b ON b.id = ti.book_id
           WHERE ti.transfer_id = ?
           ORDER BY ti.id""",
        (transfer_id,),
    ).fetchall()
    data = dict(t)
    data["items"] = [dict(r) for r in items]
    return data


@router.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    data = _load_transfer(db, transfer_id)
    if not _can_view_transfer(db, user, data):
        raise HTTPException(403, "Forbidden")
    return data


def _transfer_next_step(status) -> str:
    return {
        "requested": "Waiting for approval",
        "approved": "Ready to dispatch",
        "dispatched": "In transit — confirm receipt",
        "received": "Completed",
        "rejected": "Rejected",
    }.get(status, status or "")


def _transfer_print_steps(t) -> str:
    def step(label, at, by, cls):
        if at:
            detail = format_ist_datetime(at) + (f" · {by}" if by else "")
        else:
            detail = "Next step" if "current" in cls.split() else "Pending"
        mark = "✓ " if "done" in cls.split() else ""
        return (f"<div class='step {cls}'><span class='label'>{mark}{escape(label)}</span>"
                f"<span class='date'>{escape(detail)}</span></div>")

    if t["status"] == "rejected":
        return ("<div class='steps'>"
                + step("Requested", t["requested_at"], t["requested_by_name"], "done")
                + "<div class='step rejected'><span class='label'>Rejected</span>"
                  "<span class='date'>No stock moved</span></div></div>")
    order = ["requested", "approved", "dispatched", "received"]
    index = order.index(t["status"]) if t["status"] in order else -1
    return "<div class='steps'>" + "".join([
        step("Requested", t["requested_at"], t["requested_by_name"],
             "done" if t["status"] == "received" or index > 0 else "current"),
        step("Approved", t["approved_at"], t["approved_by_name"],
             "done" if t["status"] == "received" or index > 1 else "current" if index == 1 else ""),
        step("Dispatched", t["dispatched_at"], t["dispatched_by_name"],
             "done" if t["status"] == "received" or index > 2 else "current" if index == 2 else ""),
        step("Completed", t["received_at"], t["received_by_name"],
             "done" if t["status"] == "received" else "current" if index == 3 else ""),
    ]) + "</div>"


def transfer_print_html(t) -> str:
    items = t["items"]
    total_units = sum(int(i["qty"]) for i in items)
    total_value = sum(float(i["price"] or 0) * int(i["qty"]) for i in items)
    item_rows = "".join(
        f"<tr><td>{escape(str(i['title']))}</td><td>{escape(str(i['barcode'] or ''))}</td>"
        f"<td class='r'>{int(i['qty'])}</td>"
        f"<td class='r'>{float(i['price'] or 0):.2f}</td>"
        f"<td class='r'>{float(i['price'] or 0) * int(i['qty']):.2f}</td></tr>"
        for i in items
    ) or "<tr><td colspan='5' class='hint'>No items.</td></tr>"
    note = (
        f"<tr><th>Note</th><td colspan='3'>{escape(str(t['note']))}</td></tr>"
        if t.get("note") else ""
    )
    status = escape(str(t["status"]))
    return f"""<!doctype html><html><head><meta charset="utf-8">
    <title>Transfer {escape(str(t['request_number']))}</title>
    <style>
      * {{ box-sizing: border-box; }}
      body {{ margin: 26px; color: #222; font-family: Arial, sans-serif; font-size: 12px; }}
      .head {{ display: flex; justify-content: space-between; align-items: flex-start;
               border-bottom: 2px solid #8a5a1f; padding-bottom: 10px; margin-bottom: 16px; }}
      h1 {{ margin: 0; font-size: 20px; color: #8a5a1f; }}
      .sub {{ margin: 3px 0; color: #666; font-size: 12px; }}
      .badge {{ display: inline-block; padding: 3px 10px; border-radius: 10px;
                font-size: 11px; font-weight: 700; background: #eee; color: #444; }}
      .badge.requested {{ background: #fde9b8; color: #7a5a00; }}
      .badge.approved {{ background: #d3e7fb; color: #15589c; }}
      .badge.dispatched {{ background: #e3d5f7; color: #5b32a0; }}
      .badge.received {{ background: #ead9c3; color: #6f481b; }}
      .badge.rejected {{ background: #f7d3d0; color: #8c1d16; }}
      .hint {{ color: #8a6d3b; margin: 0 0 12px; }}
      .steps {{ display: flex; gap: 8px; margin-bottom: 16px; }}
      .step {{ flex: 1; background: #faf7f0; border-left: 4px solid #d8c9a8;
               padding: 7px 9px; border-radius: 0 4px 4px 0; }}
      .step .label {{ display: block; font-size: 11px; font-weight: 700; color: #6b5a3a; }}
      .step .date {{ display: block; margin-top: 3px; font-size: 10px; color: #8a6d3b; }}
      .step.done {{ border-left-color: #6f481b; background: #f3e8d5; }}
      .step.current {{ border-left-color: #8a5a1f; background: #fdf3dd; }}
      .step.rejected {{ border-left-color: #b3261e; background: #fbeae8; }}
      table {{ width: 100%; border-collapse: collapse; margin-bottom: 14px; }}
      th, td {{ border: 1px solid #d8d0c0; padding: 7px 8px; text-align: left; }}
      th {{ background: #f5efe2; color: #5a4626; }}
      .r {{ text-align: right; }}
      h2 {{ font-size: 13px; color: #8a5a1f; margin: 16px 0 8px; }}
      .sign {{ margin-top: 34px; display: flex; gap: 70px; font-size: 11px; color: #444; }}
      .sign div {{ border-top: 1px solid #999; padding-top: 6px; min-width: 220px; }}
      .actions {{ display: flex; justify-content: flex-end; margin-bottom: 10px; }}
      .actions button {{ border: 0; border-radius: 6px; background: #8a5a1f; color: #fff;
                         padding: 9px 16px; font: 600 13px Arial, sans-serif; cursor: pointer; }}
      @media print {{ body {{ margin: 10mm; }} .actions {{ display: none; }} }}
    </style></head><body onload="window.print()">
    <div class="actions"><button onclick="window.print()">Print</button></div>
    <div class="head">
      <div>
        <h1>ISKCON Jamnagar Book Management</h1>
        <div class="sub">Stock transfer &mdash; {escape(str(t['request_number']))}</div>
        <div class="sub">Raised {escape(format_ist_datetime(t['requested_at']))}
          by {escape(str(t['requested_by_name'] or 'unknown'))}</div>
      </div>
      <div><span class="badge {status}">{status.upper()}</span></div>
    </div>
    <p class="hint">{escape(_transfer_next_step(t['status']))}</p>
    {_transfer_print_steps(t)}
    <table>
      <tr><th>Route</th><td colspan="3">{escape(str(t['from_name']))} &rarr; {escape(str(t['to_name']))}</td></tr>
      <tr><th>Items</th><td colspan="3">{len(items)} line{'s' if len(items) != 1 else ''} &middot; {total_units} units</td></tr>
      {note}
    </table>
    <h2>Books</h2>
    <table>
      <tr><th>Title</th><th>Barcode</th><th class="r">Qty</th><th class="r">Price</th><th class="r">Value</th></tr>
      {item_rows}
      <tr><th colspan="2" class="r">Total</th><th class="r">{total_units}</th><th></th>
        <th class="r">{total_value:.2f}</th></tr>
    </table>
    <div class="sign">
      <div>Issued by (warehouse)</div>
      <div>Received by (destination)</div>
    </div>
    </body></html>"""


@router.get("/transfers/{transfer_id}/print", response_class=HTMLResponse)
def print_transfer(transfer_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    data = _load_transfer(db, transfer_id)
    if not _can_view_transfer(db, user, data):
        raise HTTPException(403, "Forbidden")
    return transfer_print_html(data)


@router.delete("/transfers/{transfer_id}")
def delete_transfer(transfer_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    t = db.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
    if not t:
        raise HTTPException(404, "Transfer not found")
    if not _can_view_transfer(db, user, t):
        raise HTTPException(403, "Forbidden")
    if t["status"] not in ("requested", "rejected", "received"):
        raise HTTPException(409, "Cannot delete: transfer is already in progress or completed")
    if t["status"] == "received":
        if not can_view(db, user, t["from_location_id"]) or not can_view(db, user, t["to_location_id"]):
            raise HTTPException(403, "Forbidden")

    if t["status"] == "received":
        items = db.execute(
            """SELECT book_id, SUM(qty) AS qty
               FROM transfer_items WHERE transfer_id = ? GROUP BY book_id""",
            (transfer_id,),
        ).fetchall()
        shortages = []
        for item in items:
            row = db.execute(
                "SELECT qty FROM stock WHERE book_id = ? AND location_id = ?",
                (item["book_id"], t["to_location_id"]),
            ).fetchone()
            available = row["qty"] if row else 0
            if available < item["qty"]:
                book = db.execute("SELECT title FROM books WHERE id = ?", (item["book_id"],)).fetchone()
                shortages.append(f"{book['title']} (need {item['qty']}, have {available})")
        if shortages:
            raise HTTPException(
                409,
                "Cannot delete received transfer: insufficient destination stock to reverse: "
                + ", ".join(shortages),
            )
        for item in items:
            move_stock(
                db, item["book_id"], t["to_location_id"], -item["qty"],
                "TRANSFER_REVERSED_OUT", "transfers", transfer_id, user["id"],
                "received transfer deleted", commit=False,
            )
            move_stock(
                db, item["book_id"], t["from_location_id"], item["qty"],
                "TRANSFER_REVERSED_IN", "transfers", transfer_id, user["id"],
                "received transfer deleted", commit=False,
            )
    db.execute("DELETE FROM transfer_items WHERE transfer_id = ?", (transfer_id,))
    db.execute("DELETE FROM transfers WHERE id = ?", (transfer_id,))
    db.commit()
    details = t["request_number"]
    if t["status"] == "received":
        details += " (received stock movement reversed)"
    log_audit(db, user["id"], "TRANSFER_DELETE", "transfers", transfer_id, details, user["username"])
    return {"ok": True}


@router.post("/transfers")
def request_transfer(data: TransferIn, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
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

    num = f"TRF-{uuid4().hex[:12].upper()}"
    cur = db.execute(
        """INSERT INTO transfers (request_number, from_location_id, to_location_id, status, requested_by, note)
           VALUES (?,?,?, 'requested', ?, ?)""",
        (num, data.from_location_id, data.to_location_id, user["id"], data.note),
    )
    tid = cur.lastrowid
    num = f"TRF-{tid:04d}"
    db.execute("UPDATE transfers SET request_number = ? WHERE id = ?", (num, tid))
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
    if not _can_view_transfer(db, user, t):
        raise HTTPException(403, "Forbidden")
    if t["status"] != "requested":
        raise HTTPException(400, f"Cannot approve transfer in status '{t['status']}'")
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
    if not _can_view_transfer(db, user, t):
        raise HTTPException(403, "Forbidden")
    if t["status"] != "requested":
        raise HTTPException(400, f"Cannot reject transfer in status '{t['status']}'")
    db.execute("UPDATE transfers SET status = 'rejected' WHERE id = ?", (transfer_id,))
    db.commit()
    log_audit(db, user["id"], "TRANSFER_REJECT", "transfers", transfer_id, t["request_number"], user["username"])
    return {"ok": True, "status": "rejected"}


@router.post("/transfers/{transfer_id}/dispatch")
def dispatch_transfer(transfer_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    t = db.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
    if not t:
        raise HTTPException(404, "Transfer not found")
    if user["role"] == "zone_admin" and not _can_view_transfer(db, user, t):
        raise HTTPException(403, "Forbidden")
    if user["role"] != "super_admin" and not can_view(db, user, t["from_location_id"]):
        raise HTTPException(403, "Forbidden")
    if t["status"] != "approved":
        raise HTTPException(400, f"Cannot dispatch transfer in status '{t['status']}'")
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
def receive_transfer(transfer_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    t = db.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
    if not t:
        raise HTTPException(404, "Transfer not found")
    if user["role"] != "super_admin" and not can_view(db, user, t["to_location_id"]):
        raise HTTPException(403, "Forbidden")
    if t["status"] != "dispatched":
        raise HTTPException(400, f"Cannot receive transfer in status '{t['status']}'")
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
def list_returns(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    ids = location_ids(db, user)
    sql = """SELECT r.*, l.code AS location_code, l.name AS location_name,
                    (SELECT COUNT(*) FROM return_items WHERE return_id = r.id) AS item_count,
                    (SELECT COALESCE(SUM(qty),0) FROM return_items WHERE return_id = r.id) AS units
             FROM returns r
             JOIN locations l ON l.id = r.location_id
             WHERE r.location_id IN ({})
             ORDER BY r.id DESC""".format(",".join("?" * len(ids)))
    return [dict(r) for r in db.execute(sql, list(ids)).fetchall()]


@router.get("/returns/{return_id}")
def get_return(return_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
    r = db.execute(
        """SELECT r.*, l.code AS location_code, l.name AS location_name,
                  cl.name AS credit_location_name,
                  cu.username AS created_by_name, pu.username AS processed_by_name
           FROM returns r
           JOIN locations l ON l.id = r.location_id
           LEFT JOIN locations cl ON cl.id = r.credit_location_id
           LEFT JOIN users cu ON cu.id = r.created_by
           LEFT JOIN users pu ON pu.id = r.processed_by
           WHERE r.id = ?""",
        (return_id,),
    ).fetchone()
    if not r:
        raise HTTPException(404, "Return not found")
    if r["location_id"] not in location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    items = db.execute(
        """SELECT ri.qty, b.title, b.barcode, b.price
           FROM return_items ri JOIN books b ON b.id = ri.book_id
           WHERE ri.return_id = ?""",
        (return_id,),
    ).fetchall()
    data = dict(r)
    data["items"] = [dict(item) for item in items]
    return data


@router.post("/returns")
def create_return(data: ReturnIn, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
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
    num = f"RET-{now_ist().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:12].upper()}"
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
def list_damaged(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "temple"))):
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
