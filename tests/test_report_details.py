import sqlite3
import unittest

from fastapi import HTTPException

from app.database import SCHEMA
from app.routers.reports import (
    sales_by_book,
    stock_summary_books,
    transfer_status_details,
    transfers_report,
)


class ReportDetailsTests(unittest.TestCase):
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
            """INSERT INTO books (id, title, barcode, price)
               VALUES (?, ?, ?, ?)""",
            [
                (1, "Book A", "111", 100),
                (2, "Book B", "222", 200),
            ],
        )
        self.db.execute(
            "INSERT INTO stock (book_id, location_id, qty) VALUES (1, 2, 5)"
        )
        self.db.executemany(
            """INSERT INTO invoices
                   (id, invoice_number, location_id, payment_mode, subtotal, discount, total, status)
               VALUES (?, ?, ?, 'cash', ?, 0, ?, 'paid')""",
            [
                (1, "INV-A", 2, 200, 200),
                (2, "INV-B", 3, 300, 300),
            ],
        )
        self.db.executemany(
            """INSERT INTO invoice_items
                   (invoice_id, book_id, qty, unit_price, line_total)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (1, 1, 2, 100, 200),
                (2, 2, 1, 300, 300),
            ],
        )
        self.db.executemany(
            """INSERT INTO transfers
                   (id, request_number, from_location_id, to_location_id, status)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (1, "TRF-A", 1, 2, "requested"),
                (2, "TRF-B", 1, 3, "requested"),
                (3, "TRF-OUT", 2, 1, "approved"),
            ],
        )
        self.db.executemany(
            "INSERT INTO transfer_items (transfer_id, book_id, qty) VALUES (?, ?, ?)",
            [(1, 1, 4), (1, 2, 3), (2, 2, 8), (3, 1, 2)],
        )
        self.db.commit()
        self.zone_admin = {"role": "zone_admin", "zone_id": 2}
        self.super_admin = {"role": "super_admin", "zone_id": None}

    def tearDown(self):
        self.db.close()

    def test_stock_details_include_books_with_zero_stock(self):
        details = stock_summary_books(2, db=self.db, user=self.zone_admin)

        self.assertEqual(details["location"]["name"], "Zone A")
        self.assertEqual(
            [(book["title"], book["qty"]) for book in details["books"]],
            [("Book A", 5), ("Book B", 0)],
        )

    def test_stock_details_reject_locations_outside_user_scope(self):
        with self.assertRaises(HTTPException) as error:
            stock_summary_books(3, db=self.db, user=self.zone_admin)

        self.assertEqual(error.exception.status_code, 403)

    def test_sales_book_breakdown_is_limited_to_selected_location(self):
        books = sales_by_book(location_id=2, db=self.db, user=self.zone_admin)

        self.assertEqual([(book["title"], book["qty_sold"]) for book in books], [("Book A", 2)])
        self.assertEqual(sales_by_book(location_id=3, db=self.db, user=self.zone_admin), [])

    def test_zone_admin_transfer_summary_and_details_only_include_destination_zone(self):
        summary = transfers_report(db=self.db, user=self.zone_admin)
        details = transfer_status_details("requested", db=self.db, user=self.zone_admin)

        self.assertEqual(summary, [{"status": "requested", "n": 1}])
        self.assertEqual(details["transfer_count"], 1)
        self.assertEqual(details["total_units"], 7)
        self.assertEqual(
            [(book["title"], book["qty"]) for book in details["books"]],
            [("Book A", 4), ("Book B", 3)],
        )
        self.assertEqual([transfer["request_number"] for transfer in details["transfers"]], ["TRF-A"])

    def test_super_admin_transfer_details_include_all_matching_locations(self):
        details = transfer_status_details("requested", db=self.db, user=self.super_admin)

        self.assertEqual(details["transfer_count"], 2)
        self.assertEqual(details["total_units"], 15)


if __name__ == "__main__":
    unittest.main()
