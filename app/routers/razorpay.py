import hashlib
import hmac
import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request

from ..database import get_db
from ..services.ledger import move_stock

router = APIRouter(tags=["payments"])
logger = logging.getLogger(__name__)


def _entity(event, name):
    return (
        event.get("payload", {})
        .get(name, {})
        .get("entity", {})
    )


def _record_system_audit(db, action, invoice_id, details):
    db.execute(
        """INSERT INTO audit_logs (user_id, username, action, entity, entity_id, details)
           VALUES (NULL, 'Razorpay', ?, 'invoices', ?, ?)""",
        (action, invoice_id, details),
    )


@router.post("/api/razorpay/webhook")
async def razorpay_webhook(request: Request, db=Depends(get_db)):
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, "Razorpay webhook is not configured")

    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(400, "Invalid Razorpay webhook signature")

    event_id = request.headers.get("X-Razorpay-Event-Id", "").strip()
    if not event_id:
        raise HTTPException(400, "Missing Razorpay event ID")
    try:
        event = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(400, "Invalid webhook JSON") from error
    if not isinstance(event, dict):
        raise HTTPException(400, "Invalid webhook payload")

    event_type = event.get("event")
    cursor = db.execute(
        """INSERT OR IGNORE INTO razorpay_webhook_events (event_id, event_type)
           VALUES (?, ?)""",
        (event_id, str(event_type or "unknown")),
    )
    if cursor.rowcount == 0:
        db.commit()
        return {"ok": True, "duplicate": True}

    if event_type not in ("qr_code.credited", "qr_code.closed"):
        db.commit()
        return {"ok": True, "ignored": True}

    qr_code = _entity(event, "qr_code")
    qr_code_id = qr_code.get("id")
    if not qr_code_id:
        db.rollback()
        raise HTTPException(400, "Razorpay event is missing its QR code ID")

    invoice = db.execute(
        "SELECT * FROM invoices WHERE razorpay_qr_code_id = ?",
        (qr_code_id,),
    ).fetchone()
    if not invoice or invoice["status"] != "pending":
        db.commit()
        return {"ok": True, "ignored": True}

    if event_type == "qr_code.credited":
        payment = _entity(event, "payment")
        expected_amount = int(round(float(invoice["total"]) * 100))
        if (
            payment.get("status") != "captured"
            or payment.get("currency") != "INR"
            or payment.get("method") != "upi"
            or payment.get("amount") != expected_amount
            or not payment.get("id")
        ):
            logger.error(
                "Ignoring invalid Razorpay payment for invoice %s (payment %s)",
                invoice["invoice_number"],
                payment.get("id"),
            )
            db.commit()
            return {"ok": True, "ignored": True}

        db.execute(
            """UPDATE invoices
                  SET status = 'paid', payment_ref = ?, razorpay_payment_id = ?
                WHERE id = ? AND status = 'pending'""",
            (payment["id"], payment["id"], invoice["id"]),
        )
        _record_system_audit(
            db,
            "RAZORPAY_PAYMENT_CONFIRMED",
            invoice["id"],
            f"{invoice['invoice_number']} payment {payment['id']} amount {invoice['total']}",
        )
        db.commit()
        return {"ok": True, "paid": True}

    received_count = qr_code.get("payments_count_received")
    if received_count == 0:
        items = db.execute(
            "SELECT book_id, qty FROM invoice_items WHERE invoice_id = ?",
            (invoice["id"],),
        ).fetchall()
        for item in items:
            move_stock(
                db,
                item["book_id"],
                invoice["location_id"],
                item["qty"],
                "SALE_REVERSED",
                "invoices",
                invoice["id"],
                invoice["cashier_id"],
                "Razorpay QR closed without payment",
                commit=False,
            )
        db.execute(
            "UPDATE invoices SET status = 'failed' WHERE id = ? AND status = 'pending'",
            (invoice["id"],),
        )
        _record_system_audit(
            db,
            "RAZORPAY_PAYMENT_EXPIRED",
            invoice["id"],
            f"{invoice['invoice_number']} QR closed without payment; stock restored",
        )
        db.commit()
        return {"ok": True, "expired": True}

    if received_count is None:
        logger.warning(
            "Razorpay closed event for invoice %s is missing payments_count_received",
            invoice["invoice_number"],
        )
    db.commit()
    return {"ok": True, "ignored": True}
