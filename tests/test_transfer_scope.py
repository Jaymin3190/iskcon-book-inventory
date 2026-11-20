import sqlite3
import unittest

from fastapi import HTTPException

from app.database import SCHEMA
from app.routers.stock import approve_transfer, get_transfer, list_transfers


class TransferScopeTests(unittest.TestCase):
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
               VALUES (1, 'zone-admin', 'not-used', 'Zone Admin', 'zone_admin', 2)"""
        )
        self.db.execute(
            """INSERT INTO users (id, username, password_hash, name, role, zone_id)
               VALUES (2, 'super-admin', 'not-used', 'Super Admin', 'super_admin', NULL)"""
        )
        self.db.executemany(
            """INSERT INTO transfers
                   (id, request_number, from_location_id, to_location_id, status)
               VALUES (?, ?, 1, ?, 'requested')""",
            [
                (1, "TRF-ZONE-A", 2),
                (2, "TRF-ZONE-B", 3),
            ],
        )
        self.db.commit()
        self.zone_admin = self.db.execute(
            "SELECT * FROM users WHERE id = 1"
        ).fetchone()
        self.super_admin = self.db.execute(
            "SELECT * FROM users WHERE id = 2"
        ).fetchone()

    def tearDown(self):
        self.db.close()

    def test_zone_admin_sees_only_transfers_to_their_zone(self):
        transfers = list_transfers(db=self.db, user=self.zone_admin)

        self.assertEqual([transfer["request_number"] for transfer in transfers], ["TRF-ZONE-A"])

    def test_zone_admin_can_open_transfer_for_their_zone(self):
        transfer = get_transfer(1, db=self.db, user=self.zone_admin)

        self.assertEqual(transfer["request_number"], "TRF-ZONE-A")

    def test_zone_admin_cannot_approve_any_transfer(self):
        for transfer_id in (1, 2):
            with self.assertRaises(HTTPException) as approve_error:
                approve_transfer(transfer_id, db=self.db, user=self.zone_admin)
            self.assertEqual(approve_error.exception.status_code, 403)
        statuses = self.db.execute(
            "SELECT status FROM transfers ORDER BY id"
        ).fetchall()
        self.assertEqual([row["status"] for row in statuses], ["requested", "requested"])

    def test_super_admin_can_approve_any_transfer(self):
        for transfer_id in (1, 2):
            result = approve_transfer(transfer_id, db=self.db, user=self.super_admin)
            self.assertEqual(result["status"], "approved")

        statuses = self.db.execute(
            "SELECT status FROM transfers ORDER BY id"
        ).fetchall()
        self.assertEqual([row["status"] for row in statuses], ["approved", "approved"])

    def test_zone_admin_cannot_open_another_zones_transfer(self):
        with self.assertRaises(HTTPException) as detail_error:
            get_transfer(2, db=self.db, user=self.zone_admin)
        self.assertEqual(detail_error.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
