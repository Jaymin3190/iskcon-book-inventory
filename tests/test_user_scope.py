import sqlite3
import unittest

from fastapi import HTTPException

from app.database import SCHEMA
from app.routers.users import UserIn, UserPatch, create_user, delete_user, update_user


class UserCreationScopeTests(unittest.TestCase):
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
                (2, "super-admin", "Super Admin", "super_admin", None),
                (3, "zone-a-distributor", "Zone A Distributor", "distributor", 2),
                (4, "zone-b-distributor", "Zone B Distributor", "distributor", 3),
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

    def test_zone_admin_creates_distributor_in_own_zone_without_zone_input(self):
        result = create_user(
            UserIn(
                username="new-distributor",
                password="test-password",
                name="New Distributor",
                role="distributor",
            ),
            db=self.db,
            user=self.zone_admin,
        )

        created = self.db.execute(
            "SELECT role, zone_id FROM users WHERE id = ?", (result["id"],)
        ).fetchone()
        self.assertEqual(tuple(created), ("distributor", 2))

    def test_zone_admin_cannot_create_users_in_another_zone(self):
        with self.assertRaises(HTTPException) as raised:
            create_user(
                UserIn(
                    username="outside-distributor",
                    password="test-password",
                    name="Outside Distributor",
                    role="distributor",
                    zone_id=3,
                ),
                db=self.db,
                user=self.zone_admin,
            )

        self.assertEqual(raised.exception.status_code, 403)

    def test_zone_admin_cannot_create_another_role(self):
        with self.assertRaises(HTTPException) as raised:
            create_user(
                UserIn(
                    username="extra-admin",
                    password="test-password",
                    name="Extra Admin",
                    role="zone_admin",
                    zone_id=2,
                ),
                db=self.db,
                user=self.zone_admin,
            )

        self.assertEqual(raised.exception.status_code, 403)

    def test_super_admin_can_still_create_other_roles(self):
        result = create_user(
            UserIn(
                username="new-zone-admin",
                password="test-password",
                name="New Zone Admin",
                role="zone_admin",
                zone_id=3,
            ),
            db=self.db,
            user=self.super_admin,
        )

        created = self.db.execute(
            "SELECT role, zone_id FROM users WHERE id = ?", (result["id"],)
        ).fetchone()
        self.assertEqual(tuple(created), ("zone_admin", 3))

    def test_super_admin_can_create_and_unassign_a_distributor(self):
        result = create_user(
            UserIn(
                username="godown-distributor",
                password="test-password",
                name="Godown Distributor",
                role="distributor",
            ),
            db=self.db,
            user=self.super_admin,
        )
        self.assertIsNone(
            self.db.execute(
                "SELECT zone_id FROM users WHERE id = ?", (result["id"],)
            ).fetchone()["zone_id"]
        )

        updated = update_user(
            3,
            UserPatch(zone_id=None),
            db=self.db,
            user=self.super_admin,
        )
        self.assertEqual(updated, {"ok": True})
        self.assertIsNone(
            self.db.execute("SELECT zone_id FROM users WHERE id = 3").fetchone()["zone_id"]
        )

    def test_super_admin_still_requires_zones_for_other_non_admin_roles(self):
        for role in ("zone_admin", "temple"):
            with self.subTest(role=role), self.assertRaises(HTTPException) as raised:
                create_user(
                    UserIn(
                        username=f"no-zone-{role}",
                        password="test-password",
                        name="No Zone",
                        role=role,
                    ),
                    db=self.db,
                    user=self.super_admin,
                )
            self.assertEqual(raised.exception.status_code, 400)

    def test_zone_admin_can_edit_distributor_in_own_zone(self):
        result = update_user(
            3,
            UserPatch(name="Updated Distributor", username="updated-distributor"),
            db=self.db,
            user=self.zone_admin,
        )

        updated = self.db.execute(
            "SELECT name, username, role, zone_id FROM users WHERE id = 3"
        ).fetchone()
        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            tuple(updated),
            ("Updated Distributor", "updated-distributor", "distributor", 2),
        )

    def test_zone_admin_cannot_edit_other_zone_or_change_role_or_zone(self):
        with self.assertRaises(HTTPException) as raised:
            update_user(
                4,
                UserPatch(name="Should not change"),
                db=self.db,
                user=self.zone_admin,
            )
        self.assertEqual(raised.exception.status_code, 403)

        for data in (
            UserPatch(role="zone_admin"),
            UserPatch(zone_id=3),
        ):
            with self.assertRaises(HTTPException) as raised:
                update_user(3, data, db=self.db, user=self.zone_admin)
            self.assertEqual(raised.exception.status_code, 403)

    def test_zone_admin_can_delete_distributor_in_own_zone(self):
        result = delete_user(3, db=self.db, user=self.zone_admin)

        self.assertEqual(result, {"ok": True})
        self.assertIsNone(self.db.execute("SELECT id FROM users WHERE id = 3").fetchone())

    def test_zone_admin_cannot_delete_users_outside_own_zone(self):
        with self.assertRaises(HTTPException) as raised:
            delete_user(4, db=self.db, user=self.zone_admin)

        self.assertEqual(raised.exception.status_code, 403)
        self.assertIsNotNone(self.db.execute("SELECT id FROM users WHERE id = 4").fetchone())


if __name__ == "__main__":
    unittest.main()
