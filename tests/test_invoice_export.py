import sqlite3
import unittest

from app.database import SCHEMA
from app.routers.pos import export_invoices


class InvoiceExportTests(unittest.TestCase):
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
                (2, "ZONE-A", "Zone A", "zone", 1),
                (3, "ZONE-B", "Zone B", "zone", 1),
            ],
        )
        self.db.executemany(
            """INSERT INTO users (id, username, password_hash, name, role, zone_id)
               VALUES (?, ?, 'not-used', ?, ?, ?)""",
            [
                (1, "zone-admin", "Zone Admin", "zone_admin", 2),
                (2, "distributor", "Distributor", "distributor", 2),
                (3, "other-distributor", "Other Distributor", "distributor", 3),
            ],
        )
        self.db.execute(
            "INSERT INTO books (id, title, barcode, price) VALUES (1, 'Test book', '1234567890', 25)"
        )
        self.db.executemany(
            """INSERT INTO invoices
                   (id, invoice_number, location_id, payment_mode, razorpay_qr_code_id,
                    subtotal, discount, total, cashier_id)
               VALUES (?, ?, ?, ?, ?, 50, 0, 50, ?)""",
            [
                (1, "INV-zone-a", 2, "upi", "qr_zone_a", 2),
                (2, "INV-zone-a-admin", 2, "cash", None, 1),
                (3, "INV-zone-b", 3, "cash", None, 3),
            ],
        )
        self.db.execute(
            """INSERT INTO invoice_items (invoice_id, book_id, qty, unit_price, line_total)
               VALUES (1, 1, 2, 25, 50)"""
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_zone_export_includes_item_details_and_only_visible_zone_invoices(self):
        user = self.db.execute("SELECT * FROM users WHERE id = 1").fetchone()

        rows = export_invoices(db=self.db, user=user)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["invoice_number"], "INV-zone-a-admin")
        self.assertEqual(rows[1]["invoice_number"], "INV-zone-a")
        self.assertEqual(rows[1]["payment_mode"], "razorpay")
        self.assertEqual(rows[1]["book_title"], "Test book")
        self.assertEqual(rows[1]["barcode"], "1234567890")
        self.assertEqual(rows[1]["qty"], 2)
        self.assertNotIn("INV-zone-b", {row["invoice_number"] for row in rows})

    def test_distributor_export_contains_only_their_own_invoices(self):
        user = self.db.execute("SELECT * FROM users WHERE id = 2").fetchone()

        rows = export_invoices(db=self.db, user=user)

        self.assertEqual([row["invoice_number"] for row in rows], ["INV-zone-a"])


if __name__ == "__main__":
    unittest.main()
