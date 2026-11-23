import sqlite3
import unittest

from fastapi import HTTPException

from app.database import SCHEMA
from app.routers import finance, pos


class DuplicateBillTests(unittest.TestCase):
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
        self.db.executemany(
            """INSERT INTO users (id, username, password_hash, name, role, zone_id)
               VALUES (?, ?, 'not-used', ?, ?, ?)""",
            [
                (1, "admin", "Admin", "super_admin", None),
                (2, "zoneadmin", "Zone admin", "zone_admin", 2),
            ],
        )
        self.db.execute(
            """INSERT INTO books (id, title, barcode, price, cost_price)
               VALUES (1, 'Test book', '1234567890', 150, 90)"""
        )
        self.db.execute(
            "INSERT INTO stock (book_id, location_id, qty) VALUES (1, 2, 20)"
        )
        self.db.commit()
        self.admin = self.db.execute("SELECT * FROM users WHERE id = 1").fetchone()

    def tearDown(self):
        self.db.close()

    def _invoice(self, request_id):
        data = pos.InvoiceIn(
            location_id=2,
            customer_name="Customer",
            customer_phone="9999999999",
            payment_mode="cash",
            client_request_id=request_id,
            items=[pos.InvoiceItemIn(barcode="1234567890", qty=2)],
        )
        return pos.create_invoice(data, db=self.db, user=self.admin)

    def _purchase(self, request_id, bill_number="B-1"):
        data = finance.PurchaseIn(
            supplier="Supplier",
            bill_number=bill_number,
            location_id=1,
            purchase_date="2026-11-10",
            amount_paid=0,
            client_request_id=request_id,
            items=[finance.PurchaseItemIn(book_id=1, qty=5, unit_cost=80)],
        )
        return finance.create_purchase(data, db=self.db, user=self.admin)

    def test_a_retried_sale_returns_the_first_bill(self):
        first = self._invoice("sale-request-1")
        second = self._invoice("sale-request-1")

        self.assertEqual(second["invoice_id"], first["invoice_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) AS n FROM invoices").fetchone()["n"], 1
        )
        stock = self.db.execute(
            "SELECT qty FROM stock WHERE book_id = 1 AND location_id = 2"
        ).fetchone()
        self.assertEqual(stock["qty"], 18)

    def test_a_retried_purchase_does_not_add_stock_twice(self):
        first = self._purchase("purchase-request-1")
        second = self._purchase("purchase-request-1")

        self.assertEqual(second["purchase_id"], first["purchase_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) AS n FROM purchases").fetchone()["n"], 1
        )
        stock = self.db.execute(
            "SELECT qty FROM stock WHERE book_id = 1 AND location_id = 1"
        ).fetchone()
        self.assertEqual(stock["qty"], 5)

    def test_the_same_supplier_bill_cannot_be_entered_twice(self):
        self._purchase("purchase-request-2", bill_number="SUP-77")

        with self.assertRaises(HTTPException) as raised:
            self._purchase("purchase-request-3", bill_number="SUP-77")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("already recorded", raised.exception.detail)

    def test_a_retried_expense_creates_one_entry(self):
        def save(request_id):
            data = finance.ExpenseIn(
                expense_date="2026-11-10",
                category="Rent",
                amount=50,
                location_id=1,
                client_request_id=request_id,
            )
            return finance.create_expense(data, db=self.db, user=self.admin)

        first = save("expense-request-1")
        second = save("expense-request-1")

        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) AS n FROM expenses").fetchone()["n"], 1
        )


if __name__ == "__main__":
    unittest.main()
