import sqlite3
import unittest

from fastapi import HTTPException

from app.database import SCHEMA
from app.routers.stock import StockAdjustBatchIn, adjust_stock_batch


class StockAdjustBatchTests(unittest.TestCase):
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
               VALUES (1, 'temple', 'not-used', 'Temple', 'temple', 2)"""
        )
        self.db.executemany(
            "INSERT INTO books (id, title, barcode) VALUES (?, ?, ?)",
            [(1, "First book", "111"), (2, "Second book", "222")],
        )
        self.db.executemany(
            "INSERT INTO stock (book_id, location_id, qty) VALUES (?, 1, ?)",
            [(1, 5), (2, 1)],
        )
        self.db.commit()
        self.user = self.db.execute("SELECT * FROM users WHERE id = 1").fetchone()

    def tearDown(self):
        self.db.close()

    def test_batch_adjusts_multiple_books_and_records_movements(self):
        result = adjust_stock_batch(
            StockAdjustBatchIn(
                location_id=1,
                items=[
                    {"book_id": 1, "qty": 3},
                    {"book_id": 2, "qty": 4},
                ],
            ),
            db=self.db,
            user=self.user,
        )

        self.assertEqual(result["items"], [
            {"book_id": 1, "qty": 8},
            {"book_id": 2, "qty": 5},
        ])
        movements = self.db.execute(
            "SELECT book_id, qty, change_type FROM stock_movements ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in movements],
            [(1, 3, "STOCK_IN"), (2, 4, "STOCK_IN")],
        )
        audit_count = self.db.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        self.assertEqual(audit_count, 1)

    def test_batch_failure_rolls_back_every_book(self):
        with self.assertRaises(HTTPException) as raised:
            adjust_stock_batch(
                StockAdjustBatchIn(
                    location_id=1,
                    items=[
                        {"book_id": 1, "qty": -2},
                        {"book_id": 2, "qty": -2},
                    ],
                ),
                db=self.db,
                user=self.user,
            )

        self.assertEqual(raised.exception.status_code, 400)
        quantities = self.db.execute(
            "SELECT book_id, qty FROM stock WHERE location_id = 1 ORDER BY book_id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in quantities], [(1, 5), (2, 1)])
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
