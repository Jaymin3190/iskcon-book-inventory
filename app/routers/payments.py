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

RECORD_ROLES = ("super_admin", "temple")
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


class PaymentPreviewIn(BaseModel):
    user_id: int
    receipt_numbers: list[str]


class PaymentIn(BaseModel):
    user_id: int
    receipt_numbers: list[str]
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


def _resolve_receipts(db, zone_user, numbers):
    """Turn receipt numbers into handover lines, with friendly per-number errors.

    A zone leader hands over the cash for the whole zone team, so a receipt may
    have been billed by the leader or by any distributor in that zone. Temple and
    super-admin bills are excluded because that cash is already with the temple.
    """
    unique = []
    seen = set()
    for raw in numbers:
        number = str(raw or "").strip()
        if number and number not in seen:
            seen.add(number)
            unique.append(number)
    if not unique:
        return [], ["Add at least one receipt number"]

    ids = zone_location_ids(db, zone_user["zone_id"])
    placeholders = ",".join("?" * len(ids))
    rows = db.execute(
        f"""SELECT i.id, i.invoice_number, i.created_at, i.total, i.payment_mode,
                   i.cashier_id, l.name AS location_name,
                   u.name AS cashier_name, u.role AS cashier_role,
                   u.zone_id AS cashier_zone_id,
                   (SELECT 1 FROM cash_submission_items c WHERE c.invoice_id = i.id) AS handed_over
              FROM invoices i
              JOIN locations l ON l.id = i.location_id
              LEFT JOIN users u ON u.id = i.cashier_id
             WHERE i.invoice_number IN ({",".join("?" * len(unique))})
               AND i.location_id IN ({placeholders})""",
        unique + list(ids),
    ).fetchall()
    by_number = {r["invoice_number"]: r for r in rows}

    items = []
    errors = []
    for number in unique:
        row = by_number.get(number)
        if not row:
            errors.append(f"{number}: no such receipt in this zone")
        elif row["payment_mode"] != "cash":
            errors.append(f"{number}: is a {row['payment_mode'].upper()} receipt, not cash")
        elif row["handed_over"]:
            errors.append(f"{number}: already handed over to the temple")
        elif row["cashier_role"] not in ("zone_admin", "distributor"):
            errors.append(f"{number}: was billed by the temple, not by the zone team")
        elif row["cashier_zone_id"] != zone_user["zone_id"]:
            errors.append(f"{number}: belongs to another zone")
        else:
            items.append({
                "invoice_id": row["id"],
                "invoice_number": row["invoice_number"],
                "created_at": row["created_at"],
                "amount": round(float(row["total"]), 2),
                "location_name": row["location_name"],
                "cashier_name": row["cashier_name"],
                "cashier_role": row["cashier_role"],
            })
    return items, errors


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
        """SELECT csi.invoice_number, csi.amount, i.created_at,
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
    data["items"] = [dict(it) for it in items]
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
    """Zone admins this temple may receive cash from."""
    ids = payment_zone_ids(db, user)
    rows = db.execute(
        """SELECT u.id, u.name, u.username, u.zone_id, z.name AS zone_name
             FROM users u
             JOIN locations z ON z.id = u.zone_id
            WHERE u.role = 'zone_admin' AND u.is_active = 1
              AND u.zone_id IN ({})
            ORDER BY z.name, u.name""".format(",".join("?" * len(ids))),
        list(ids),
    ).fetchall()
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

    ids = zone_location_ids(db, zone_user["zone_id"])
    rows = db.execute(
        """SELECT i.id, i.invoice_number, i.created_at, i.total,
                  l.name AS location_name,
                  u.name AS cashier_name, u.role AS cashier_role
             FROM invoices i
             JOIN locations l ON l.id = i.location_id
             LEFT JOIN users u ON u.id = i.cashier_id
            WHERE i.payment_mode = 'cash'
              AND i.location_id IN ({})
              AND u.role IN ('zone_admin', 'distributor')
              AND u.zone_id = ?
              AND NOT EXISTS (SELECT 1 FROM cash_submission_items c WHERE c.invoice_id = i.id)
            ORDER BY i.id""".format(",".join("?" * len(ids))),
        list(ids) + [zone_user["zone_id"]],
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/preview")
def preview_payment(data: PaymentPreviewIn, db=Depends(get_db), user=Depends(require_roles(*RECORD_ROLES))):
    """Auto-sum entered receipt numbers so the temple admin can verify the cash."""
    zone_user = _zone_admin(db, data.user_id, payment_zone_ids(db, user))
    items, errors = _resolve_receipts(db, zone_user, data.receipt_numbers)
    return {
        "items": items,
        "errors": errors,
        "count": len(items),
        "total": round(sum(i["amount"] for i in items), 2),
    }


@router.post("")
def create_payment(data: PaymentIn, db=Depends(get_db), user=Depends(require_roles(*RECORD_ROLES))):
    zone_user = _zone_admin(db, data.user_id, payment_zone_ids(db, user))
    items, errors = _resolve_receipts(db, zone_user, data.receipt_numbers)
    if errors:
        raise HTTPException(400, " ".join(errors))
    if not items:
        raise HTTPException(400, "Add at least one receipt number")

    total = round(sum(i["amount"] for i in items), 2)
    number = next_receipt_number(db, user)
    try:
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
    except sqlite3.IntegrityError:
        db.rollback()
        raise HTTPException(409, "One of those receipts was just handed over by someone else. Reload and try again.")

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
        f"<td class='r'>{float(it['amount']):.2f}</td></tr>"
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
      <h1>ISKCON Book Distribution</h1>
      <div class="sub">Cash payment-in receipt &mdash; {escape(str(payment['receipt_number']))}</div>
      <div class="sub">Date: {escape(format_ist_datetime(payment['created_at']))}</div>
    </div>
    <p class="sub">Received from: <strong>{escape(str(payment['zone_admin_name']))}</strong>
      ({escape(str(payment['zone_admin_username']))})</p>
    <p class="sub">Zone: {escape(str(payment['zone_name']))}
      &nbsp;|&nbsp; Confirmed by: {escape(str(payment['recorded_by_name']))}</p>
    <table>
      <tr><th>Receipt</th><th>Billed by</th><th>Billed at</th><th class="r">Amount</th></tr>
      {rows}
      <tr class="total"><td colspan="3" class="r">Total cash received</td>
        <td class="r">{float(payment['amount']):.2f}</td></tr>
    </table>
    {note}
    <p class="sub">This receipt confirms that the temple has received the cash for the
      bills listed above. Status: {escape(str(payment['status']).upper())}.</p>
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
