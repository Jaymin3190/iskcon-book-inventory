import sqlite3
import unittest

from fastapi import HTTPException

from app.database import SCHEMA
from app.routers.reports import (
    distributor_details,
    distributor_sales_report,
    distributors_report,
    sales_by_book,
    stock_summary_books,
    transfer_status_details,
    transfers_report,
    zone_overview,
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
        self.db.execute(
            """INSERT INTO users (id, username, password_hash, name, role, zone_id)
               VALUES (1, 'dist-a', 'not-used', 'Distributor A', 'distributor', 2)"""
        )
        self.db.execute(
            """INSERT INTO users (id, username, password_hash, name, role, zone_id)
               VALUES (5, 'godown-dist', 'not-used', 'Godown Distributor', 'distributor', NULL)"""
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
        self.db.execute("UPDATE invoices SET cashier_id = 1 WHERE id = 1")
        self.db.execute(
            """INSERT INTO invoices
                   (id, invoice_number, location_id, payment_mode, subtotal, discount, total,
                    status, distributor_id)
               VALUES (3, 'INV-GODOWN', 1, 'cash', 900, 0, 900, 'paid', 1)"""
        )
        self.db.execute(
            """INSERT INTO invoice_items
                   (invoice_id, book_id, qty, unit_price, line_total)
               VALUES (3, 1, 9, 100, 900)"""
        )
        self.db.execute(
            """INSERT INTO invoices
                   (id, invoice_number, location_id, payment_mode, subtotal, discount, total,
                    status, distributor_id)
               VALUES (4, 'INV-GODOWN-UNASSIGNED', 1, 'cash', 400, 0, 400, 'paid', 5)"""
        )
        self.db.execute(
            """INSERT INTO invoice_items
                   (invoice_id, book_id, qty, unit_price, line_total)
               VALUES (4, 2, 2, 200, 400)"""
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

    def test_pending_to_temple_is_cash_collected_minus_handed_over(self):
        self.db.execute(
            """INSERT INTO users (id, username, password_hash, name, role, zone_id)
               VALUES (2, 'zone-a-admin', 'not-used', 'Zone A Admin', 'zone_admin', 2)"""
        )
        self.db.execute(
            """INSERT INTO users (id, username, password_hash, name, role, zone_id)
               VALUES (6, 'temple-user', 'not-used', 'Temple User', 'temple', NULL)"""
        )
        self.db.executemany(
            """INSERT INTO invoices
                   (id, invoice_number, location_id, payment_mode, subtotal, discount, total,
                    status, cashier_id, distributor_id)
               VALUES (?, ?, 2, ?, ?, 0, ?, 'paid', ?, ?)""",
            [
                (5, "INV-CASH", "cash", 1000, 1000, 1, 1),
                (6, "INV-UPI", "upi", 500, 500, 1, 1),
                (7, "INV-TEMPLE", "cash", 700, 700, 6, None),
            ],
        )
        self.db.executemany(
            """INSERT INTO invoice_payments (invoice_id, amount, method)
               VALUES (?, ?, ?)""",
            [(5, 1000, "cash"), (6, 500, "upi"), (7, 700, "cash")],
        )
        self.db.execute(
            """INSERT INTO cash_submissions
                   (id, receipt_number, zone_id, submitted_by, recorded_by, amount, invoice_count)
               VALUES (1, 'RCV-1', 2, 1, 1, 300, 1)"""
        )
        self.db.execute(
            """INSERT INTO cash_submission_items
                   (submission_id, invoice_id, invoice_number, amount)
               VALUES (1, 5, 'INV-CASH', 300)"""
        )
        self.db.commit()

        distributors = {row["id"]: row for row in distributors_report(db=self.db, user=self.super_admin)}
        details = distributor_details(1, db=self.db, user=self.super_admin)
        zones = {row["id"]: row for row in zone_overview(db=self.db, user=self.super_admin)}

        # ₹1000 cash collected on INV-CASH, ₹300 already handed over, UPI and
        # temple-billed cash excluded.
        self.assertEqual(distributors[1]["pending_to_temple"], 700.0)
        self.assertEqual(details["summary"]["pending_to_temple"], 700.0)
        self.assertEqual(zones[2]["pending_cash"], 700.0)
        # A distributor without a zone never owes a zone handover.
        self.assertEqual(distributors[5]["pending_to_temple"], 0.0)

    def test_godown_sale_is_attributed_to_selected_distributor_and_zone(self):
        distributors = distributors_report(db=self.db, user=self.super_admin)
        details = distributor_details(1, db=self.db, user=self.super_admin)
        zones = {row["id"]: row for row in zone_overview(db=self.db, user=self.super_admin)}

        distributor_by_id = {row["id"]: row for row in distributors}
        self.assertEqual(distributor_by_id[1]["books_sold"], 11)
        self.assertEqual(distributor_by_id[5]["books_sold"], 2)
        self.assertIsNone(distributor_by_id[5]["zone_name"])
        self.assertEqual(details["summary"]["books_sold"], 11)
        self.assertEqual(details["books"][0]["qty"], 11)
        self.assertEqual(zones[2]["books_sold"], 2)
        self.assertEqual(len(distributor_sales_report(db=self.db, user=self.super_admin)), 3)

        zone_admin = {"role": "zone_admin", "zone_id": 2}
        zone_sales = distributor_sales_report(db=self.db, user=zone_admin)
        self.assertEqual([sale["invoice_number"] for sale in zone_sales], ["INV-A"])


if __name__ == "__main__":
    unittest.main()
