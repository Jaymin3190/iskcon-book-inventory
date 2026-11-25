import sqlite3
import unittest

from app.database import SCHEMA
from app.routers import pos
from app.routers.reports import (
    distributor_details,
    distributor_sales_report,
    distributors_report,
    zone_overview,
)


class ZoneLeaderAsDistributorTests(unittest.TestCase):
    """A zone leader may also distribute books and hold a bill in their name."""

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
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (1, "root", "not-used", "Root", "super_admin", None),
                (2, "leader-a", "not-used", "Leader A", "zone_admin", 2),
                (3, "dist-a", "not-used", "Distributor A", "distributor", 2),
                (4, "leader-b", "not-used", "Leader B", "zone_admin", 3),
            ],
        )
        self.db.execute(
            """INSERT INTO books (id, title, barcode, price)
               VALUES (1, 'Book A', '111', 100)"""
        )
        self.db.execute(
            "INSERT INTO stock (book_id, location_id, qty) VALUES (1, 2, 20)"
        )
        self.db.commit()
        self.super_admin = self.db.execute(
            "SELECT * FROM users WHERE id = 1"
        ).fetchone()
        self.zone_leader = self.db.execute(
            "SELECT * FROM users WHERE id = 2"
        ).fetchone()

    def tearDown(self):
        self.db.close()

    def bill(self, user, distributor_id=None, payment_mode="cash", amount_received=None):
        return pos.create_invoice(
            pos.InvoiceIn(
                location_id=None if user["role"] != "super_admin" else 2,
                distributor_id=distributor_id,
                customer_name="Customer",
                customer_phone="9999999999",
                payment_mode=payment_mode,
                amount_received=amount_received,
                items=[pos.InvoiceItemIn(barcode="111", qty=1)],
            ),
            db=self.db,
            user=user,
        )

    def test_zone_leader_can_be_the_distributor_on_a_bill(self):
        # A super admin bills a zone sale in the leader's name.
        assigned = self.bill(self.super_admin, distributor_id=2)
        # The leader can also bill themselves as the distributor and collect cash.
        own = self.bill(self.zone_leader, distributor_id=2, amount_received=40)
        walk_in = self.bill(self.zone_leader)

        rows = {
            row["invoice_number"]: row
            for row in self.db.execute(
                "SELECT invoice_number, cashier_id, distributor_id, status FROM invoices"
            ).fetchall()
        }
        self.assertEqual(rows[assigned["invoice_number"]]["cashier_id"], 1)
        self.assertEqual(rows[assigned["invoice_number"]]["distributor_id"], 2)
        self.assertEqual(rows[own["invoice_number"]]["cashier_id"], 2)
        self.assertEqual(rows[own["invoice_number"]]["distributor_id"], 2)
        # An ordinary walk-in bill is not a leader payable.
        self.assertIsNone(rows[walk_in["invoice_number"]]["distributor_id"])
        self.assertEqual(rows[walk_in["invoice_number"]]["status"], "paid")

        # Only the two explicitly credited bills count for the leader.
        distributors = {
            row["id"]: row for row in distributors_report(db=self.db, user=self.super_admin)
        }
        self.assertIn(2, distributors)
        self.assertEqual(distributors[2]["books_sold"], 2)
        self.assertEqual(distributors[2]["revenue"], 200)
        self.assertEqual(distributors[2]["pending_to_temple"], 40)

        details = distributor_details(2, db=self.db, user=self.super_admin)
        self.assertEqual(details["summary"]["invoice_count"], 2)
        self.assertEqual(details["summary"]["books_sold"], 2)
        self.assertEqual(details["summary"]["pending_to_temple"], 40)
        detail_numbers = {invoice["invoice_number"] for invoice in details["invoices"]}
        self.assertEqual(
            detail_numbers, {assigned["invoice_number"], own["invoice_number"]}
        )
        self.assertNotIn(walk_in["invoice_number"], detail_numbers)

        sales = distributor_sales_report(db=self.db, user=self.super_admin)
        self.assertEqual({sale["distributor_name"] for sale in sales}, {"Leader A"})
        self.assertEqual(len(sales), 2)

        zones = {zone["id"]: zone for zone in zone_overview(db=self.db, user=self.super_admin)}
        # Zone A counts its distributor and the leader who now distributes.
        self.assertEqual(zones[2]["distributor_count"], 2)
        # Zone B's leader has no distributor bills, so they are not counted.
        self.assertEqual(zones[3]["distributor_count"], 0)

    def test_zone_leader_can_hold_a_credit_bill(self):
        result = self.bill(self.zone_leader, distributor_id=2, payment_mode="ledger")

        self.assertEqual(result["status"], "unpaid")
        self.assertEqual(result["balance_due"], 100)
        details = distributor_details(2, db=self.db, user=self.super_admin)
        self.assertEqual(details["summary"]["ledger_total"], 100)

    def test_leader_from_another_zone_cannot_be_selected(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as raised:
            self.bill(self.zone_leader, distributor_id=4)

        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
