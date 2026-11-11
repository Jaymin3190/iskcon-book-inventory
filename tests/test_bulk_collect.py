import sqlite3
import unittest

from fastapi import HTTPException

from app.database import SCHEMA
from app.routers import pos


class BulkCollectionTests(unittest.TestCase):
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
                (3, "OTHER", "Other zone", "zone", 1),
            ],
        )
        self.db.executemany(
            """INSERT INTO users (id, username, password_hash, name, role, zone_id)
               VALUES (?, ?, 'not-used', ?, ?, ?)""",
            [
                (1, "admin", "Admin", "super_admin", None),
                (2, "dist", "Distributor", "distributor", 2),
                (3, "zoneadmin", "Zone admin", "zone_admin", 2),
                (4, "otherzone", "Other zone admin", "zone_admin", 3),
                (5, "dist2", "Second distributor", "distributor", 2),
                (6, "cashier", "Cashier", "temple", 2),
            ],
        )
        self.db.execute(
            """INSERT INTO books (id, title, barcode, price)
               VALUES (1, 'Test book', '1234567890', 150.25)"""
        )
        self.db.execute(
            "INSERT INTO stock (book_id, location_id, qty) VALUES (1, 2, 40)"
        )
        self.db.commit()
        self.admin = self.db.execute("SELECT * FROM users WHERE id = 1").fetchone()
        self.zone_admin = self.db.execute("SELECT * FROM users WHERE id = 3").fetchone()
        self.other_zone_admin = self.db.execute("SELECT * FROM users WHERE id = 4").fetchone()

    def tearDown(self):
        self.db.close()

    def create_bill(self, distributor_id=2):
        data = pos.InvoiceIn(
            location_id=2,
            distributor_id=distributor_id,
            customer_name="Customer",
            customer_phone="9999999999",
            payment_mode="cash",
            items=[pos.InvoiceItemIn(barcode="1234567890", qty=1)],
        )
        return pos.create_invoice(data, db=self.db, user=self.admin)

    def collect(self, invoice_ids, user=None, **kwargs):
        data = pos.InvoiceBulkPaymentIn(invoice_ids=invoice_ids, **kwargs)
        return pos.collect_invoice_payments(data, db=self.db, user=user or self.zone_admin)

    def test_collects_several_bills_in_one_action(self):
        first = self.create_bill()
        second = self.create_bill()
        third = self.create_bill()

        result = self.collect([first["invoice_id"], second["invoice_id"], third["invoice_id"]])

        self.assertEqual(result["invoice_count"], 3)
        self.assertEqual(result["total_collected"], 450.75)
        self.assertEqual(result["balance_due"], 0)
        self.assertEqual(
            [payment["status"] for payment in result["payments"]],
            ["paid", "paid", "paid"],
        )
        saved = self.db.execute(
            "SELECT status FROM invoices ORDER BY id"
        ).fetchall()
        self.assertEqual([row["status"] for row in saved], ["paid", "paid", "paid"])
        payments = self.db.execute(
            "SELECT invoice_id, amount, method FROM invoice_payments ORDER BY invoice_id"
        ).fetchall()
        self.assertEqual(len(payments), 3)
        self.assertTrue(all(row["method"] == "cash" for row in payments))

    def test_selected_bills_are_allocated_oldest_first_on_a_part_payment(self):
        first = self.create_bill()
        second = self.create_bill()
        # Later bill is passed first to prove the oldest bill is still paid first.
        result = self.collect(
            [second["invoice_id"], first["invoice_id"]],
            amount=200.25,
        )

        self.assertEqual(result["total_collected"], 200.25)
        self.assertEqual(result["balance_due"], 100.25)
        by_invoice = {p["invoice_id"]: p for p in result["payments"]}
        self.assertEqual(by_invoice[first["invoice_id"]]["amount"], 150.25)
        self.assertEqual(by_invoice[first["invoice_id"]]["status"], "paid")
        self.assertEqual(by_invoice[second["invoice_id"]]["amount"], 50.0)
        self.assertEqual(by_invoice[second["invoice_id"]]["status"], "partial")
        self.assertEqual(by_invoice[second["invoice_id"]]["balance_due"], 100.25)

    def test_bills_from_one_distributor_only(self):
        self.create_bill(distributor_id=2)
        second = self.create_bill(distributor_id=5)
        first = self.db.execute(
            "SELECT id FROM invoices ORDER BY id LIMIT 1"
        ).fetchone()["id"]

        with self.assertRaises(HTTPException) as raised:
            self.collect([first, second["invoice_id"]])

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("one distributor", raised.exception.detail)

    def test_bills_outside_the_zone_are_rejected(self):
        bill = self.create_bill()

        with self.assertRaises(HTTPException) as raised:
            self.collect([bill["invoice_id"]], user=self.other_zone_admin)

        self.assertEqual(raised.exception.status_code, 403)
        paid = self.db.execute(
            "SELECT COUNT(*) FROM invoice_payments"
        ).fetchone()[0]
        self.assertEqual(paid, 0)

    def test_overpayment_and_empty_selection_are_rejected(self):
        bill = self.create_bill()

        with self.assertRaises(HTTPException) as empty:
            self.collect([])
        self.assertEqual(empty.exception.status_code, 400)

        with self.assertRaises(HTTPException) as over:
            self.collect([bill["invoice_id"]], amount=999)
        self.assertEqual(over.exception.status_code, 400)
        self.assertIn("outstanding total", over.exception.detail)

    def test_already_paid_bill_is_rejected(self):
        bill = self.create_bill()
        self.collect([bill["invoice_id"]])

        with self.assertRaises(HTTPException) as raised:
            self.collect([bill["invoice_id"]])

        self.assertEqual(raised.exception.status_code, 409)

    def test_invoice_list_names_the_distributor(self):
        self.create_bill()

        invoices = pos.list_invoices(db=self.db, user=self.zone_admin)

        self.assertEqual(len(invoices), 1)
        self.assertEqual(invoices[0]["distributor_name"], "Distributor")


if __name__ == "__main__":
    unittest.main()
