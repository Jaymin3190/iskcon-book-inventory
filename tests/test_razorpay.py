import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from starlette.requests import Request
from fastapi import HTTPException

from app.database import SCHEMA, _migrate_razorpay
from app.routers import pos
from app.routers.reports import sales_report, zone_overview
from app.routers.razorpay import razorpay_webhook
from app.services.razorpay import RazorpayError, create_qr_code


class RazorpayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self.db.executemany(
            """INSERT INTO locations (id, code, name, type, parent_id)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (1, "WH", "Warehouse", "warehouse", None),
                (2, "ZONE", "Zone", "zone", 1),
            ],
        )
        self.db.execute(
            """INSERT INTO users (id, username, password_hash, name, role, zone_id)
               VALUES (1, 'cashier', 'not-used', 'Cashier', 'temple', 2)"""
        )
        self.db.execute(
            """INSERT INTO books (id, title, barcode, price)
               VALUES (1, 'Test book', '1234567890', 150.25)"""
        )
        self.db.execute(
            "INSERT INTO stock (book_id, location_id, qty) VALUES (1, 2, 5)"
        )
        self.db.commit()
        self.user = self.db.execute("SELECT * FROM users WHERE id = 1").fetchone()

    def tearDown(self):
        self.db.close()

    def create_pending_invoice(self):
        data = pos.InvoiceIn(
            customer_name="Test customer",
            customer_phone="9999999999",
            payment_mode="razorpay",
            items=[pos.InvoiceItemIn(barcode="1234567890", qty=1)],
        )
        with patch.dict(
            os.environ,
            {"RAZORPAY_KEY_ID": "test-key", "RAZORPAY_KEY_SECRET": "test-secret"},
        ), patch.object(
            pos,
            "create_qr_code",
            return_value={
                "id": "qr_test123",
                "image_url": "https://rzp.io/i/test123",
            },
        ):
            result = pos.create_invoice(data, db=self.db, user=self.user)
        return result

    def send_webhook(self, event, event_id, signature_secret="webhook-secret"):
        body = json.dumps(event, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(
            signature_secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/razorpay/webhook",
            "raw_path": b"/api/razorpay/webhook",
            "query_string": b"",
            "headers": [
                (b"x-razorpay-signature", signature.encode("ascii")),
                (b"x-razorpay-event-id", event_id.encode("ascii")),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 443),
        }

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(scope, receive)
        with patch.dict(os.environ, {"RAZORPAY_WEBHOOK_SECRET": "webhook-secret"}):
            return asyncio.run(razorpay_webhook(request, db=self.db))

    def test_razorpay_invoice_is_pending_and_reserves_stock(self):
        result = self.create_pending_invoice()
        invoice = self.db.execute(
            "SELECT * FROM invoices WHERE id = ?", (result["invoice_id"],)
        ).fetchone()
        stock = self.db.execute(
            "SELECT qty FROM stock WHERE book_id = 1 AND location_id = 2"
        ).fetchone()["qty"]
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["payment_mode"], "razorpay")
        self.assertEqual(result["razorpay_qr_code_id"], "qr_test123")
        self.assertEqual(invoice["razorpay_qr_image_url"], "https://rzp.io/i/test123")
        self.assertEqual(invoice["payment_mode"], "upi")
        self.assertEqual(stock, 4)

    def test_razorpay_is_rejected_without_razorpay_credentials(self):
        data = pos.InvoiceIn(
            customer_name="Test customer",
            customer_phone="9999999999",
            payment_mode="razorpay",
            items=[pos.InvoiceItemIn(barcode="1234567890", qty=1)],
        )
        with patch.dict(
            os.environ,
            {"RAZORPAY_KEY_ID": "", "RAZORPAY_KEY_SECRET": ""},
        ), self.assertRaises(HTTPException) as raised:
            pos.create_invoice(data, db=self.db, user=self.user)
        stock = self.db.execute(
            "SELECT qty FROM stock WHERE book_id = 1 AND location_id = 2"
        ).fetchone()["qty"]
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(stock, 5)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 0)

    def test_existing_upi_mode_saves_as_paid_without_razorpay(self):
        data = pos.InvoiceIn(
            customer_name="Test customer",
            customer_phone="9999999999",
            payment_mode="upi",
            items=[pos.InvoiceItemIn(barcode="1234567890", qty=1)],
        )
        with patch.dict(
            os.environ,
            {"RAZORPAY_KEY_ID": "", "RAZORPAY_KEY_SECRET": ""},
        ), patch.object(pos, "create_qr_code") as create_qr:
            result = pos.create_invoice(data, db=self.db, user=self.user)
        saved = self.db.execute(
            "SELECT payment_mode, status, razorpay_qr_code_id FROM invoices WHERE id = ?",
            (result["invoice_id"],),
        ).fetchone()
        self.assertEqual(result["status"], "paid")
        self.assertEqual(saved["payment_mode"], "upi")
        self.assertEqual(saved["status"], "paid")
        self.assertIsNone(saved["razorpay_qr_code_id"])
        create_qr.assert_not_called()

    def test_existing_upi_mode_is_counted_separately_from_razorpay(self):
        data = pos.InvoiceIn(
            customer_name="UPI customer",
            customer_phone="9999999999",
            payment_mode="upi",
            items=[pos.InvoiceItemIn(barcode="1234567890", qty=1)],
        )
        result = pos.create_invoice(data, db=self.db, user=self.user)
        zone = next(
            row for row in zone_overview(
                db=self.db, user={"role": "super_admin", "zone_id": None}
            )
            if row["id"] == 2
        )
        self.assertEqual(result["status"], "paid")
        self.assertEqual(zone["upi_total"], 150.25)
        self.assertEqual(zone["razorpay_total"], 0)

    def test_confirmed_razorpay_is_reported_separately_from_upi(self):
        invoice = self.create_pending_invoice()
        event = {
            "event": "qr_code.credited",
            "payload": {
                "qr_code": {"entity": {"id": "qr_test123"}},
                "payment": {
                    "entity": {
                        "id": "pay_test123",
                        "status": "captured",
                        "amount": 15025,
                        "currency": "INR",
                        "method": "upi",
                    }
                },
            },
        }
        self.send_webhook(event, "event-report-razorpay")
        zone = next(
            row for row in zone_overview(
                db=self.db, user={"role": "super_admin", "zone_id": None}
            )
            if row["id"] == 2
        )
        self.assertEqual(zone["upi_total"], 0)
        self.assertEqual(zone["razorpay_total"], 150.25)

    def test_pending_invoice_is_excluded_from_sales_report(self):
        self.create_pending_invoice()
        self.assertEqual(sales_report(db=self.db, user={"role": "super_admin"}), [])

    def test_qr_creation_failure_rolls_back_invoice_and_stock(self):
        data = pos.InvoiceIn(
            customer_name="Test customer",
            customer_phone="9999999999",
            payment_mode="razorpay",
            items=[pos.InvoiceItemIn(barcode="1234567890", qty=1)],
        )
        with patch.dict(
            os.environ,
            {"RAZORPAY_KEY_ID": "test-key", "RAZORPAY_KEY_SECRET": "test-secret"},
        ), patch.object(
            pos, "create_qr_code", side_effect=RazorpayError("QR feature unavailable")
        ), self.assertRaises(HTTPException) as raised:
            pos.create_invoice(data, db=self.db, user=self.user)
        stock = self.db.execute(
            "SELECT qty FROM stock WHERE book_id = 1 AND location_id = 2"
        ).fetchone()["qty"]
        invoice_count = self.db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(invoice_count, 0)
        self.assertEqual(stock, 5)

    def test_credited_webhook_confirms_only_matching_payment_and_is_idempotent(self):
        invoice = self.create_pending_invoice()
        event = {
            "event": "qr_code.credited",
            "payload": {
                "qr_code": {"entity": {"id": "qr_test123"}},
                "payment": {
                    "entity": {
                        "id": "pay_test123",
                        "status": "captured",
                        "amount": 15025,
                        "currency": "INR",
                        "method": "upi",
                    }
                },
            },
        }
        result = self.send_webhook(event, "event-credited")
        duplicate = self.send_webhook(event, "event-credited")
        saved = self.db.execute(
            "SELECT status, razorpay_payment_id FROM invoices WHERE id = ?",
            (invoice["invoice_id"],),
        ).fetchone()
        self.assertEqual(result, {"ok": True, "paid": True})
        self.assertEqual(duplicate, {"ok": True, "duplicate": True})
        self.assertEqual(saved["status"], "paid")
        self.assertEqual(saved["razorpay_payment_id"], "pay_test123")

    def test_unpaid_closed_qr_fails_invoice_and_restores_stock_once(self):
        invoice = self.create_pending_invoice()
        event = {
            "event": "qr_code.closed",
            "payload": {
                "qr_code": {
                    "entity": {"id": "qr_test123", "payments_count_received": 0}
                }
            },
        }
        result = self.send_webhook(event, "event-closed")
        duplicate = self.send_webhook(event, "event-closed")
        saved = self.db.execute(
            "SELECT status FROM invoices WHERE id = ?", (invoice["invoice_id"],)
        ).fetchone()
        stock = self.db.execute(
            "SELECT qty FROM stock WHERE book_id = 1 AND location_id = 2"
        ).fetchone()["qty"]
        self.assertEqual(result, {"ok": True, "expired": True})
        self.assertEqual(duplicate, {"ok": True, "duplicate": True})
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(stock, 5)

    def test_closed_event_with_received_payment_does_not_restore_stock(self):
        invoice = self.create_pending_invoice()
        event = {
            "event": "qr_code.closed",
            "payload": {
                "qr_code": {
                    "entity": {"id": "qr_test123", "payments_count_received": 1}
                }
            },
        }
        result = self.send_webhook(event, "event-closed-with-payment")
        saved = self.db.execute(
            "SELECT status FROM invoices WHERE id = ?", (invoice["invoice_id"],)
        ).fetchone()
        stock = self.db.execute(
            "SELECT qty FROM stock WHERE book_id = 1 AND location_id = 2"
        ).fetchone()["qty"]
        self.assertEqual(result, {"ok": True, "ignored": True})
        self.assertEqual(saved["status"], "pending")
        self.assertEqual(stock, 4)

    def test_credited_webhook_rejects_amount_mismatch(self):
        invoice = self.create_pending_invoice()
        event = {
            "event": "qr_code.credited",
            "payload": {
                "qr_code": {"entity": {"id": "qr_test123"}},
                "payment": {
                    "entity": {
                        "id": "pay_wrong",
                        "status": "captured",
                        "amount": 15024,
                        "currency": "INR",
                        "method": "upi",
                    }
                },
            },
        }
        result = self.send_webhook(event, "event-wrong-amount")
        saved = self.db.execute(
            "SELECT status FROM invoices WHERE id = ?", (invoice["invoice_id"],)
        ).fetchone()
        self.assertEqual(result, {"ok": True, "ignored": True})
        self.assertEqual(saved["status"], "pending")

    def test_webhook_rejects_invalid_signature(self):
        event = {"event": "qr_code.closed", "payload": {"qr_code": {"entity": {"id": "qr_test123"}}}}
        with self.assertRaises(HTTPException) as raised:
            self.send_webhook(event, "event-invalid-signature", signature_secret="wrong-secret")
        self.assertEqual(raised.exception.status_code, 400)

    def test_existing_invoice_schema_gets_razorpay_migration(self):
        legacy = sqlite3.connect(":memory:")
        legacy.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY)")
        _migrate_razorpay(legacy)
        columns = {row[1] for row in legacy.execute("PRAGMA table_info(invoices)")}
        indexes = {
            row[1] for row in legacy.execute("PRAGMA index_list(invoices)")
        }
        self.assertTrue(
            {"razorpay_qr_code_id", "razorpay_qr_image_url", "razorpay_payment_id"}
            <= columns
        )
        self.assertIn("idx_invoices_razorpay_qr_code_id", indexes)
        legacy.close()

    def test_qr_request_uses_fixed_single_use_amount_in_paise(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"id":"qr_test123","image_url":"https://rzp.io/i/test123"}'

        with patch.dict(
            os.environ,
            {"RAZORPAY_KEY_ID": "test-key", "RAZORPAY_KEY_SECRET": "test-secret"},
        ), patch("app.services.razorpay.time.time", return_value=1000), patch(
            "app.services.razorpay.urllib.request.urlopen",
            return_value=FakeResponse(),
        ) as urlopen:
            result = create_qr_code("INV-test-0001", 150.25)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(result["id"], "qr_test123")
        self.assertEqual(payload["type"], "upi_qr")
        self.assertEqual(payload["usage"], "single_use")
        self.assertTrue(payload["fixed_amount"])
        self.assertEqual(payload["payment_amount"], 15025)
        self.assertEqual(payload["close_by"], 4600)


if __name__ == "__main__":
    unittest.main()
