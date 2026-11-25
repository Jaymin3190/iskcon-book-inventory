import sqlite3
import unittest
from unittest.mock import patch

from app.database import SCHEMA
from app.routers import pos


class InvoiceAreaTests(unittest.TestCase):
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
               VALUES (1, 'zoneadmin', 'not-used', 'Zone admin', 'zone_admin', 2)"""
        )
        self.db.execute(
            """INSERT INTO books (id, title, barcode, price, cost_price)
               VALUES (1, 'Test book', '1234567890', 150, 90)"""
        )
        self.db.execute(
            "INSERT INTO stock (book_id, location_id, qty) VALUES (1, 2, 20)"
        )
        self.db.commit()
        self.user = self.db.execute("SELECT * FROM users WHERE id = 1").fetchone()
        self.counter = 0

    def tearDown(self):
        self.db.close()

    def bill(self, area_name=None, latitude=None, longitude=None):
        self.counter += 1
        data = pos.InvoiceIn(
            location_id=2,
            customer_name="Customer",
            customer_phone="9999999999",
            payment_mode="cash",
            latitude=latitude,
            longitude=longitude,
            area_name=area_name,
            client_request_id=f"area-request-{self.counter}",
            items=[pos.InvoiceItemIn(barcode="1234567890", qty=1)],
        )
        result = pos.create_invoice(data, db=self.db, user=self.user)
        stored = self.db.execute(
            "SELECT area_name FROM invoices WHERE id = ?", (result["invoice_id"],)
        ).fetchone()
        return result, stored["area_name"]

    def test_client_resolved_area_is_stored_and_cleaned(self):
        result, stored = self.bill(
            area_name="  Gokul   Nagar,\n Jamnagar ", latitude=22.4707, longitude=70.0577
        )

        self.assertEqual(stored, "Gokul Nagar, Jamnagar")
        self.assertEqual(result["area_name"], "Gokul Nagar, Jamnagar")

    def test_client_area_skips_the_server_lookup(self):
        with patch.object(pos, "reverse_geocode") as lookup:
            _, stored = self.bill(
                area_name="Gokul Nagar, Jamnagar", latitude=22.4707, longitude=70.0577
            )

        self.assertEqual(stored, "Gokul Nagar, Jamnagar")
        lookup.assert_not_called()

    def test_server_lookup_is_used_when_the_client_sends_no_area(self):
        with patch.object(pos, "reverse_geocode", return_value="Gokul Nagar, Jamnagar") as lookup:
            _, stored = self.bill(latitude=22.4707, longitude=70.0577)

        self.assertEqual(stored, "Gokul Nagar, Jamnagar")
        lookup.assert_called_once_with(22.4707, 70.0577)

    def test_blank_client_area_falls_back_to_the_server(self):
        with patch.object(pos, "reverse_geocode", return_value="Gokul Nagar") as lookup:
            _, stored = self.bill(area_name="   ", latitude=22.4707, longitude=70.0577)

        self.assertEqual(stored, "Gokul Nagar")
        lookup.assert_called_once()

    def test_a_failed_server_lookup_does_not_block_the_bill(self):
        with patch.object(pos, "reverse_geocode", side_effect=OSError("blocked")):
            result, stored = self.bill(latitude=22.4707, longitude=70.0577)

        self.assertIsNone(stored)
        self.assertEqual(result["location_warning"], "The area name could not be looked up.")

    def test_a_long_client_area_is_capped(self):
        _, stored = self.bill(area_name="A" * 400, latitude=22.4707, longitude=70.0577)

        self.assertEqual(len(stored), pos.AREA_NAME_LIMIT)

    def test_no_area_or_coordinates_leaves_the_area_empty(self):
        with patch.object(pos, "reverse_geocode") as lookup:
            _, stored = self.bill()

        self.assertIsNone(stored)
        lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
