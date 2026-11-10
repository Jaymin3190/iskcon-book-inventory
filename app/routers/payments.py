import re
import sqlite3
from html import escape
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..auth import require_roles
from ..database import get_db
from ..services.audit import log_audit
from ..services.scope import payment_zone_ids, zone_location_ids
from ..services.timezone import format_ist_datetime

router = APIRouter(prefix="/api/payments", tags=["payments"])

RECORD_ROLES = ("super_admin", "temple", "zone_admin")
VIEW_ROLES = ("super_admin", "temple", "zone_admin")


def _safe_name(value, fallback: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-._")
    return name or fallback


def next_receipt_number(db, user) -> str:
    """Per-user series such as RCV-templeAdmin-0001."""
    seq = db.execute(
        """INSERT INTO payment_sequences (user_id, next_number) VALUES (?, 1)
           ON CONFLICT(user_id) DO UPDATE SET next_number = next_number + 1
           RETURNING next_number""",
        (user["id"],),
    ).fetchone()["next_number"]
    return f"RCV-{_safe_name(user['username'], 'user' + str(user['id']))}-{seq:04d}"


class PaymentAllocationIn(BaseModel):
    invoice_number: str
    amount: float


class PaymentPreviewIn(BaseModel):
    user_id: int
    receipt_numbers: list[str] = []
    amount: Optional[float] = None
    allocations: Optional[list[PaymentAllocationIn]] = None


class PaymentIn(BaseModel):
    user_id: int
    receipt_numbers: list[str] = []
    amount: Optional[float] = None
    allocations: Optional[list[PaymentAllocationIn]] = None
    note: Optional[str] = None


def _zone_admin(db, user_id: int, allowed_zone_ids: set):
    row = db.execute(
        """SELECT u.*, z.name AS zone_name
           FROM users u
           JOIN locations z ON z.id = u.zone_id
           WHERE u.id = ?""",
        (user_id,),
    ).fetchone()
    if not row or row["role"] != "zone_admin" or not row["is_active"]:
        raise HTTPException(400, "Select an active zone admin")
    if row["zone_id"] not in allowed_zone_ids:
        raise HTTPException(403, "That zone admin is outside your temple")
    return row


def _zone_user_for(db, user, user_id: int):
    """Resolve whose cash is being handed over.

    A temple or super admin may record a handover for any zone admin in their
    scope. A zone leader records only their own handover, so they must pass
    their own user id and are pinned to their own zone.
    """
    if user["role"] == "zone_admin":
        if user_id != user["id"]:
            raise HTTPException(403, "Zone leaders can only record their own cash handover")
        return _zone_admin(db, user_id, {user["zone_id"]})
    return _zone_admin(db, user_id, payment_zone_ids(db, user))


def _zone_cash_rows(db, zone_user):
    """Every invoice in the zone with its customer-paid and handed-over cash."""
    ids = zone_location_ids(db, zone_user["zone_id"])
    placeholders = ",".join("?" * len(ids))
    return db.execute(
        f"""SELECT i.id, i.invoice_number, i.created_at, i.total, i.status, i.payment_mode,
                   i.cashier_id, l.name AS location_name,
                   u.name AS cashier_name, u.role AS cashier_role,
                   u.zone_id AS cashier_zone_id,
                   COALESCE((SELECT SUM(p.amount) FROM invoice_payments p
                              WHERE p.invoice_id = i.id),0) AS amount_paid,
                   COALESCE((SELECT SUM(p.amount) FROM invoice_payments p
                              WHERE p.invoice_id = i.id AND p.method = 'cash'),0) AS cash_collected,
                   COALESCE((SELECT SUM(c.amount) FROM cash_submission_items c
                              WHERE c.invoice_id = i.id),0) AS cash_remitted
              FROM invoices i
              JOIN locations l ON l.id = i.location_id
              LEFT JOIN users u ON u.id = i.cashier_id
             WHERE i.location_id IN ({placeholders})
             ORDER BY i.created_at, i.id""",
        list(ids),
    ).fetchall()


def _cash_item(row):
    total = round(float(row["total"] or 0), 2)
    paid = round(float(row["amount_paid"] or 0), 2)
    collected = round(float(row["cash_collected"] or 0), 2)
    remitted = round(float(row["cash_remitted"] or 0), 2)
    remittable = max(0, round(collected - remitted, 2))
    return {
        "invoice_id": row["id"],
        "invoice_number": row["invoice_number"],
        "created_at": row["created_at"],
        "amount": remittable,
        "invoice_total": total,
        "amount_paid": paid,
        "balance_due": max(0, round(total - paid, 2)),
        "cash_collected": collected,
        "cash_remitted": remitted,
        "remittable": remittable,
        "location_name": row["location_name"],
        "cashier_id": row["cashier_id"],
        "cashier_name": row["cashier_name"],
        "cashier_role": row["cashier_role"],
    }


def _receipt_error(row, zone_id):
    if row["cashier_role"] not in ("zone_admin", "distributor"):
        return "was billed by the temple, not by the zone team"
    if row["cashier_zone_id"] != zone_id:
        return "belongs to another zone"
    remittable = round(float(row["cash_collected"] or 0) - float(row["cash_remitted"] or 0), 2)
    if remittable <= 0:
        if float(row["cash_collected"] or 0) > 0:
            return "has already been handed over to the temple"
        return "has no cash collected yet"
    return None


def _resolve_receipts(db, zone_user, numbers, amount=None, allocations=None):
    """Turn receipt numbers or a handed-over amount into allocation lines.

    A zone leader hands over the cash for the whole zone team, so a receipt may
    have been billed by the leader or by any distributor in that zone. Only cash
    that the zone team has actually collected and not yet handed over is
    remittable; a bill can therefore be handed over in several instalments.

    Without an amount the selected receipts are handed over in full. With an
    amount, the money is allocated oldest-bill-first across the selected
    receipts (or across the whole zone when no receipts are named).
    """
    rows = _zone_cash_rows(db, zone_user)
    by_number = {r["invoice_number"]: r for r in rows}
    zone_id = zone_user["zone_id"]

    def eligible(row):
        return _receipt_error(row, zone_id) is None

    def cash_item(row, allocated=None):
        item = _cash_item(row)
        if allocated is not None:
            item["amount"] = max(0, round(float(allocated), 2))
        return item

    errors = []
    if allocations:
        if amount is not None:
            return [], ["Choose either a handed-over amount or per-receipt amounts, not both"]
        items = []
        allocated_by_number = {}
        for allocation in allocations:
            number = str(allocation.invoice_number or "").strip()
            row = by_number.get(number)
            if not row:
                errors.append(f"{number or 'Receipt'}: no such receipt in this zone")
                continue
            problem = _receipt_error(row, zone_id)
            if problem:
                errors.append(f"{number}: {problem}")
                continue
            requested = round(float(allocation.amount), 2)
            if requested <= 0:
                errors.append(f"{number}: handover amount must be positive")
                continue
            already = allocated_by_number.get(number, 0)
            available = round(item_remittable(row) - already, 2)
            if requested > available + 0.009:
                errors.append(
                    f"{number}: only {max(0, available):.2f} cash is available to hand over"
                )
                continue
            allocated_by_number[number] = round(already + requested, 2)
            items.append(cash_item(row, requested))
        items.sort(key=lambda item: (item["created_at"] or "", item["invoice_id"]))
        return items, errors

    unique = []
    seen = set()
    for raw in numbers:
        number = str(raw or "").strip()
        if number and number not in seen:
            seen.add(number)
            unique.append(number)

    if amount is not None:
        amount = round(float(amount), 2)
        if amount <= 0:
            return [], ["Handed-over amount must be positive"]

    if unique:
        candidates = []
        for number in unique:
            row = by_number.get(number)
            if not row:
                errors.append(f"{number}: no such receipt in this zone")
                continue
            problem = _receipt_error(row, zone_id)
            if problem:
                errors.append(f"{number}: {problem}")
                continue
            candidates.append(row)
    elif amount is not None:
        candidates = [row for row in rows if eligible(row)]
    else:
        return [], ["Add at least one receipt number or a handed-over amount"]

    candidates.sort(key=lambda row: (row["created_at"] or "", row["id"]))
    items = []
    remaining = amount
    for row in candidates:
        available = item_remittable(row)
        if amount is None:
            items.append(cash_item(row, available))
            continue
        if remaining <= 0:
            break
        allocated = min(available, remaining)
        if allocated > 0:
            items.append(cash_item(row, allocated))
            remaining = round(remaining - allocated, 2)

    if amount is not None and remaining > 0.009:
        available = round(sum(item_remittable(row) for row in candidates), 2)
        errors.append(
            f"Handed-over amount is {amount - remaining:.2f} more than the "
            f"available cash of {available:.2f}"
        )
    return items, errors


def item_remittable(row):
    return max(
        0,
        round(float(row["cash_collected"] or 0) - float(row["cash_remitted"] or 0), 2),
    )


def _submission_detail(db, payment_id: int):
    row = db.execute(
        """SELECT cs.*, z.name AS zone_name, z.code AS zone_code,
                  u.name AS zone_admin_name, u.username AS zone_admin_username,
                  r.name AS recorded_by_name, r.username AS recorded_by_username
             FROM cash_submissions cs
             JOIN locations z ON z.id = cs.zone_id
             JOIN users u ON u.id = cs.submitted_by
             JOIN users r ON r.id = cs.recorded_by
            WHERE cs.id = ?""",
        (payment_id,),
    ).fetchone()
    if not row:
        return None
    items = db.execute(
        """SELECT csi.invoice_number, csi.amount, i.created_at, i.total AS invoice_total,
                  COALESCE((SELECT SUM(p.amount) FROM invoice_payments p
                             WHERE p.invoice_id = i.id),0) AS amount_paid,
                  l.name AS location_name,
                  u.name AS cashier_name, u.role AS cashier_role
             FROM cash_submission_items csi
             JOIN invoices i ON i.id = csi.invoice_id
             JOIN locations l ON l.id = i.location_id
             LEFT JOIN users u ON u.id = i.cashier_id
            WHERE csi.submission_id = ?
            ORDER BY csi.id""",
        (payment_id,),
    ).fetchall()
    data = dict(row)
    item_rows = []
    for item in items:
        item_data = dict(item)
        item_data["balance_due"] = max(
            0,
            round(
                float(item_data["invoice_total"] or 0)
                - float(item_data["amount_paid"] or 0),
                2,
            ),
        )
        item_rows.append(item_data)
    data["items"] = item_rows
    return data


def _authorize_view(db, user, payment) -> None:
    if user["role"] == "zone_admin":
        if payment["submitted_by"] != user["id"]:
            raise HTTPException(403, "Zone admins can only view their own cash handovers")
        return
    if payment["zone_id"] not in payment_zone_ids(db, user):
        raise HTTPException(403, "Forbidden")


@router.get("/zone-admins")
def list_zone_admins(db=Depends(get_db), user=Depends(require_roles(*RECORD_ROLES))):
    """Zone admins this user may receive cash from.

    A zone leader only ever receives their own handover, so the list is just
    themselves.
    """
    ids = payment_zone_ids(db, user)
    sql = """SELECT u.id, u.name, u.username, u.zone_id, z.name AS zone_name
               FROM users u
               JOIN locations z ON z.id = u.zone_id
              WHERE u.role = 'zone_admin' AND u.is_active = 1
                AND u.zone_id IN ({})""".format(",".join("?" * len(ids)))
    params = list(ids)
    if user["role"] == "zone_admin":
        sql += " AND u.id = ?"
        params.append(user["id"])
    sql += " ORDER BY z.name, u.name"
    rows = db.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


@router.get("/pending")
def pending_receipts(user_id: int, db=Depends(get_db), user=Depends(require_roles(*VIEW_ROLES))):
    """Zone-team cash bills still waiting to be handed over by a zone leader."""
    if user["role"] == "zone_admin":
        if user_id != user["id"]:
            raise HTTPException(403, "Zone admins can only view their own pending cash")
        zone_user = user
    else:
        zone_user = _zone_admin(db, user_id, payment_zone_ids(db, user))

    pending = []
    for row in _zone_cash_rows(db, zone_user):
        if row["status"] in ("pending", "failed"):
            continue
        if _receipt_error(row, zone_user["zone_id"]) is not None:
            continue
        pending.append(_cash_item(row))
    return pending


@router.post("/preview")
def preview_payment(data: PaymentPreviewIn, db=Depends(get_db), user=Depends(require_roles(*RECORD_ROLES))):
    """Auto-sum entered receipts or allocate a handed-over amount."""
    zone_user = _zone_user_for(db, user, data.user_id)
    items, errors = _resolve_receipts(
        db, zone_user, data.receipt_numbers, data.amount, data.allocations
    )
    return {
        "items": items,
        "errors": errors,
        "count": len(items),
        "total": round(sum(i["amount"] for i in items), 2),
        "available": round(
            sum(
                item_remittable(row)
                for row in _zone_cash_rows(db, zone_user)
                if _receipt_error(row, zone_user["zone_id"]) is None
            ),
            2,
        ),
    }


@router.post("")
def create_payment(data: PaymentIn, db=Depends(get_db), user=Depends(require_roles(*RECORD_ROLES))):
    zone_user = _zone_user_for(db, user, data.user_id)
    try:
        db.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as error:
        raise HTTPException(409, "Another payment-in is being recorded. Try again.") from error
    try:
        items, errors = _resolve_receipts(
            db, zone_user, data.receipt_numbers, data.amount, data.allocations
        )
        if errors:
            db.rollback()
            raise HTTPException(400, " ".join(errors))
        if not items:
            db.rollback()
            raise HTTPException(400, "Add a receipt number or a handed-over amount")

        total = round(sum(i["amount"] for i in items), 2)
        number = next_receipt_number(db, user)
        cur = db.execute(
            """INSERT INTO cash_submissions
                   (receipt_number, zone_id, submitted_by, recorded_by,
                    amount, invoice_count, note, status)
               VALUES (?,?,?,?,?,?,?,'confirmed')""",
            (number, zone_user["zone_id"], zone_user["id"], user["id"],
             total, len(items), (data.note or "").strip() or None),
        )
        payment_id = cur.lastrowid
        for it in items:
            db.execute(
                """INSERT INTO cash_submission_items
                       (submission_id, invoice_id, invoice_number, amount)
                   VALUES (?,?,?,?)""",
                (payment_id, it["invoice_id"], it["invoice_number"], it["amount"]),
            )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except sqlite3.Error:
        db.rollback()
        raise

    log_audit(
        db, user["id"], "PAYMENT_IN", "cash_submissions", payment_id,
        f"{number} from {zone_user['username']} total {total:.2f} ({len(items)} receipts)",
        user["username"],
    )
    return _submission_detail(db, payment_id)


@router.get("")
def list_payments(db=Depends(get_db), user=Depends(require_roles(*VIEW_ROLES))):
    base = """SELECT cs.*, z.name AS zone_name, z.code AS zone_code,
                     u.name AS zone_admin_name, u.username AS zone_admin_username,
                     r.name AS recorded_by_name
                FROM cash_submissions cs
                JOIN locations z ON z.id = cs.zone_id
                JOIN users u ON u.id = cs.submitted_by
                JOIN users r ON r.id = cs.recorded_by"""
    if user["role"] == "zone_admin":
        rows = db.execute(
            base + " WHERE cs.submitted_by = ? ORDER BY cs.id DESC", (user["id"],)
        ).fetchall()
    else:
        ids = payment_zone_ids(db, user)
        rows = db.execute(
            base + " WHERE cs.zone_id IN ({}) ORDER BY cs.id DESC".format(",".join("?" * len(ids))),
            list(ids),
        ).fetchall()
    return [dict(r) for r in rows]


def receipt_html(payment) -> str:
    rows = "".join(
        f"<tr><td>{escape(str(it['invoice_number']))}</td>"
        f"<td>{escape(str(it.get('cashier_name') or '-'))}</td>"
        f"<td>{escape(format_ist_datetime(it['created_at']))}</td>"
        f"<td class='r'>{float(it.get('invoice_total') or 0):.2f}</td>"
        f"<td class='r'>{float(it.get('amount_paid') or 0):.2f}</td>"
        f"<td class='r'>{float(it['amount']):.2f}</td>"
        f"<td class='r'>{float(it.get('balance_due') or 0):.2f}</td></tr>"
        for it in payment["items"]
    )
    note = (
        f"<p class='sub'>Note: {escape(str(payment['note']))}</p>"
        if payment["note"] else ""
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
    <title>Cash receipt {escape(str(payment['receipt_number']))}</title>
    <style>
      body {{ font-family: Arial, sans-serif; margin: 32px; color: #222; }}
      .head {{ border-bottom: 2px solid #8a5a1f; padding-bottom: 12px; margin-bottom: 20px; }}
      h1 {{ margin: 0; font-size: 22px; color: #8a5a1f; }}
      .sub {{ color: #666; font-size: 13px; margin: 3px 0; }}
      table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
      th, td {{ border: 1px solid #ddd; padding: 8px; font-size: 13px; text-align: left; }}
      th {{ background: #f5efe2; }}
      .r {{ text-align: right; }}
      .total {{ font-size: 15px; font-weight: bold; }}
      .sign {{ margin-top: 48px; display: flex; gap: 60px; font-size: 12px; color: #444; }}
      .sign div {{ border-top: 1px solid #999; padding-top: 6px; min-width: 220px; }}
      @media print {{ body {{ margin: 8mm; }} }}
    </style></head><body>
    <div class="head">
      <h1>ISKCON Jamnagar Book Management</h1>
      <div class="sub">Cash payment-in receipt &mdash; {escape(str(payment['receipt_number']))}</div>
      <div class="sub">Date: {escape(format_ist_datetime(payment['created_at']))}</div>
    </div>
    <p class="sub">Received from: <strong>{escape(str(payment['zone_admin_name']))}</strong>
      ({escape(str(payment['zone_admin_username']))})</p>
    <p class="sub">Zone: {escape(str(payment['zone_name']))}
      &nbsp;|&nbsp; Recorded by: {escape(str(payment['recorded_by_name']))}</p>
    <table>
      <tr><th>Receipt</th><th>Billed by</th><th>Billed at</th>
        <th class="r">Invoice total</th><th class="r">Invoice paid</th>
        <th class="r">This handover</th><th class="r">Balance due</th></tr>
      {rows}
      <tr class="total"><td colspan="5" class="r">Total cash received</td>
        <td class="r">{float(payment['amount']):.2f}</td><td></td></tr>
    </table>
    {note}
    <p class="sub">This receipt confirms that the temple has received the cash shown above.
      A bill may still have a customer balance due; that remains with the zone team.
      Status: {escape(str(payment['status']).upper())}.</p>
    <div class="sign">
      <div>Received by (temple)</div>
      <div>Handed over by (zone leader)</div>
    </div>
    </body></html>"""


@router.get("/{payment_id}")
def get_payment(payment_id: int, db=Depends(get_db), user=Depends(require_roles(*VIEW_ROLES))):
    payment = _submission_detail(db, payment_id)
    if not payment:
        raise HTTPException(404, "Payment not found")
    _authorize_view(db, user, payment)
    return payment


@router.get("/{payment_id}/print", response_class=HTMLResponse)
def print_payment(payment_id: int, db=Depends(get_db), user=Depends(require_roles(*VIEW_ROLES))):
    payment = _submission_detail(db, payment_id)
    if not payment:
        raise HTTPException(404, "Payment not found")
    _authorize_view(db, user, payment)
    return receipt_html(payment)
