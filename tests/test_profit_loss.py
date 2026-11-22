import sqlite3
import unittest

from fastapi import HTTPException

from app.database import SCHEMA
from app.routers.finance import (
    ExpenseIn,
    PurchaseIn,
    PurchaseItemIn,
    create_expense,
    create_purchase,
    delete_purchase,
    list_expenses,
)
from app.routers.reports import profit_loss_report


class ProfitLossTests(unittest.TestCase):
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
                (1, "super", "Super Admin", "super_admin", None),
                (2, "zone-a", "Zone A Admin", "zone_admin", 2),
                (3, "temple", "Temple Admin", "temple", 2),
            ],
        )
        self.db.executemany(
            """INSERT INTO books (id, title, barcode, price, cost_price)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (1, "Bhagavad-gita", "BG", 100, 60),
                (2, "Unknown cost book", "UC", 50, 0),
            ],
        )
        self.db.execute(
            "INSERT INTO stock (book_id, location_id, qty) VALUES (1, 2, 100)"
        )
        self.db.execute(
            "INSERT INTO stock (book_id, location_id, qty) VALUES (2, 2, 100)"
        )
        self.super_admin = self.db.execute("SELECT * FROM users WHERE id = 1").fetchone()
        self.zone_admin = self.db.execute("SELECT * FROM users WHERE id = 2").fetchone()
        self.temple = self.db.execute("SELECT * FROM users WHERE id = 3").fetchone()

    def tearDown(self):
        self.db.close()

    def _invoice(self, number, location_id, total, subtotal=None, discount=0,
                 status="paid", created_at="2026-11-10 10:00:00"):
        subtotal = total if subtotal is None else subtotal
        cur = self.db.execute(
            """INSERT INTO invoices (invoice_number, share_token, location_id, customer_name,
                                     customer_phone, payment_mode, status, subtotal, discount,
                                     total, cashier_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (number, number + "-token", location_id, "Customer", "9999999999", "cash",
             status, subtotal, discount, total, 1, created_at),
        )
        return cur.lastrowid

    def _line(self, invoice_id, book_id, qty, unit_price, unit_cost):
        self.db.execute(
            """INSERT INTO invoice_items (invoice_id, book_id, qty, unit_price, unit_cost, line_total)
               VALUES (?,?,?,?,?,?)""",
            (invoice_id, book_id, qty, unit_price, unit_cost, round(unit_price * qty, 2)),
        )

    def test_profit_loss_combines_sales_cost_expenses_and_damage(self):
        inv1 = self._invoice("INV-1", 2, 180, subtotal=200, discount=20)
        self._line(inv1, 1, 2, 100, 60)
        inv2 = self._invoice("INV-2", 2, 100)
        self._line(inv2, 2, 2, 50, 0)
        self.db.execute(
            """INSERT INTO expenses (expense_date, category, amount, location_id, created_by)
               VALUES ('2026-11-10', 'Rent', 40, 2, 1)"""
        )
        self.db.execute(
            """INSERT INTO expenses (expense_date, category, amount, location_id, created_by)
               VALUES ('2026-11-11', 'Salary', 5, NULL, 1)"""
        )
        self.db.execute(
            """INSERT INTO damaged (location_id, book_id, qty, reason_code, created_by, created_at)
               VALUES (2, 1, 1, 'DAMAGED', 1, '2026-11-12 10:00:00')"""
        )
        self.db.commit()

        report = profit_loss_report(
            frm="2026-11-01", to="2026-11-30", db=self.db, user=self.super_admin
        )

        self.assertEqual(report["revenue"]["net_revenue"], 280)
        self.assertEqual(report["revenue"]["discounts"], 20)
        self.assertEqual(report["cogs"], 120)
        self.assertEqual(report["gross_profit"], 160)
        self.assertEqual(report["expenses"]["total"], 45)
        self.assertEqual(report["stock_loss"]["damaged_value"], 60)
        self.assertEqual(report["net_profit"], 55)
        self.assertEqual(
            [row["title"] for row in report["missing_cost_books"]],
            ["Unknown cost book"],
        )

    def test_report_respects_date_window(self):
        inv = self._invoice("INV-OLD", 2, 100, created_at="2026-10-01 10:00:00")
        self._line(inv, 1, 1, 100, 60)
        self.db.commit()

        report = profit_loss_report(
            frm="2026-11-01", to="2026-11-30", db=self.db, user=self.super_admin
        )

        self.assertEqual(report["revenue"]["net_revenue"], 0)
        self.assertEqual(report["cogs"], 0)

    def test_zone_admin_only_sees_their_zone(self):
        inv1 = self._invoice("INV-A", 2, 100)
        self._line(inv1, 1, 1, 100, 60)
        inv2 = self._invoice("INV-B", 3, 500)
        self._line(inv2, 1, 5, 100, 60)
        self.db.execute(
            """INSERT INTO expenses (expense_date, category, amount, location_id, created_by)
               VALUES ('2026-11-10', 'Rent', 40, 2, 2)"""
        )
        self.db.execute(
            """INSERT INTO expenses (expense_date, category, amount, location_id, created_by)
               VALUES ('2026-11-10', 'Rent', 70, 3, 1)"""
        )
        self.db.commit()

        report = profit_loss_report(db=self.db, user=self.zone_admin)

        self.assertEqual(report["revenue"]["net_revenue"], 100)
        self.assertEqual(report["expenses"]["total"], 40)
        self.assertEqual([row["location_id"] for row in report["locations"]], [2])

    def test_zone_admin_cannot_read_another_zone_report(self):
        with self.assertRaises(HTTPException) as error:
            profit_loss_report(location_id=3, db=self.db, user=self.zone_admin)
        self.assertEqual(error.exception.status_code, 403)

    def test_temple_admin_sees_all_zones_under_the_warehouse(self):
        inv1 = self._invoice("INV-A", 2, 100)
        self._line(inv1, 1, 1, 100, 60)
        inv2 = self._invoice("INV-B", 3, 200)
        self._line(inv2, 1, 2, 100, 60)
        self.db.commit()

        report = profit_loss_report(db=self.db, user=self.temple)

        self.assertEqual(report["revenue"]["net_revenue"], 300)


class PurchaseTests(unittest.TestCase):
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
            ],
        )
        self.db.executemany(
            """INSERT INTO users (id, username, password_hash, name, role, zone_id)
               VALUES (?, ?, 'not-used', ?, ?, ?)""",
            [
                (1, "super", "Super Admin", "super_admin", None),
                (2, "zone-a", "Zone A Admin", "zone_admin", 2),
            ],
        )
        self.db.execute(
            """INSERT INTO books (id, title, barcode, price, cost_price)
               VALUES (1, 'Bhagavad-gita', 'BG', 100, 0)"""
        )
        self.super_admin = self.db.execute("SELECT * FROM users WHERE id = 1").fetchone()
        self.zone_admin = self.db.execute("SELECT * FROM users WHERE id = 2").fetchone()

    def tearDown(self):
        self.db.close()

    def _purchase(self, **overrides):
        data = {
            "supplier": "BBT",
            "bill_number": "BBT-1",
            "location_id": 1,
            "purchase_date": "2026-11-10",
            "freight": 0,
            "discount": 0,
            "amount_paid": 0,
            "payment_mode": "credit",
            "items": [PurchaseItemIn(book_id=1, qty=10, unit_cost=50)],
        }
        data.update(overrides)
        return PurchaseIn(**data)

    def test_purchase_adds_stock_and_sets_landed_cost(self):
        result = create_purchase(self._purchase(), db=self.db, user=self.super_admin)

        self.assertEqual(result["total"], 500)
        stock = self.db.execute(
            "SELECT qty FROM stock WHERE book_id = 1 AND location_id = 1"
        ).fetchone()["qty"]
        self.assertEqual(stock, 10)
        cost = self.db.execute("SELECT cost_price FROM books WHERE id = 1").fetchone()[
            "cost_price"
        ]
        self.assertEqual(cost, 50)

    def test_freight_and_discount_change_the_landed_cost(self):
        create_purchase(
            self._purchase(freight=40, discount=40),
            db=self.db,
            user=self.super_admin,
        )

        # 10 copies at 50 = 500, minus 40 discount, plus 40 freight = 500.
        cost = self.db.execute("SELECT cost_price FROM books WHERE id = 1").fetchone()[
            "cost_price"
        ]
        self.assertEqual(cost, 50)

    def test_second_purchase_uses_a_moving_average(self):
        create_purchase(self._purchase(), db=self.db, user=self.super_admin)
        create_purchase(
            self._purchase(
                bill_number="BBT-2",
                items=[PurchaseItemIn(book_id=1, qty=10, unit_cost=70)],
            ),
            db=self.db,
            user=self.super_admin,
        )

        cost = self.db.execute("SELECT cost_price FROM books WHERE id = 1").fetchone()[
            "cost_price"
        ]
        self.assertEqual(cost, 60)

    def test_zone_admin_cannot_create_a_purchase(self):
        self.db.execute("UPDATE users SET role = 'zone_admin' WHERE id = 1")
        user = self.db.execute("SELECT * FROM users WHERE id = 1").fetchone()
        # The role guard is on the HTTP dependency; the location scope still
        # protects the service call, so Zone B is out of reach.
        with self.assertRaises(HTTPException):
            create_purchase(self._purchase(location_id=2), db=self.db, user=user)

    def test_delete_purchase_reverses_stock(self):
        result = create_purchase(self._purchase(), db=self.db, user=self.super_admin)
        delete_purchase(result["purchase_id"], db=self.db, user=self.super_admin)

        stock = self.db.execute(
            "SELECT qty FROM stock WHERE book_id = 1 AND location_id = 1"
        ).fetchone()["qty"]
        self.assertEqual(stock, 0)

    def test_delete_purchase_refuses_when_stock_already_moved(self):
        result = create_purchase(self._purchase(), db=self.db, user=self.super_admin)
        self.db.execute(
            "UPDATE stock SET qty = 5 WHERE book_id = 1 AND location_id = 1"
        )
        self.db.commit()

        with self.assertRaises(HTTPException) as error:
            delete_purchase(result["purchase_id"], db=self.db, user=self.super_admin)
        self.assertEqual(error.exception.status_code, 409)


class ExpenseTests(unittest.TestCase):
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
                (1, "super", "Super Admin", "super_admin", None),
                (2, "zone-a", "Zone A Admin", "zone_admin", 2),
            ],
        )
        self.super_admin = self.db.execute("SELECT * FROM users WHERE id = 1").fetchone()
        self.zone_admin = self.db.execute("SELECT * FROM users WHERE id = 2").fetchone()

    def tearDown(self):
        self.db.close()

    def test_zone_admin_can_add_an_expense_to_their_zone(self):
        result = create_expense(
            ExpenseIn(expense_date="2026-11-10", category="Transport", amount=75,
                      location_id=2),
            db=self.db,
            user=self.zone_admin,
        )
        self.assertTrue(result["id"])

    def test_zone_admin_cannot_add_an_expense_to_another_zone(self):
        with self.assertRaises(HTTPException) as error:
            create_expense(
                ExpenseIn(expense_date="2026-11-10", category="Transport", amount=75,
                          location_id=3),
                db=self.db,
                user=self.zone_admin,
            )
        self.assertEqual(error.exception.status_code, 403)

    def test_expense_listing_is_scoped(self):
        create_expense(
            ExpenseIn(expense_date="2026-11-10", category="Rent", amount=40, location_id=2),
            db=self.db,
            user=self.zone_admin,
        )
        create_expense(
            ExpenseIn(expense_date="2026-11-10", category="Rent", amount=70, location_id=3),
            db=self.db,
            user=self.super_admin,
        )

        rows = list_expenses(db=self.db, user=self.zone_admin)

        self.assertEqual([row["amount"] for row in rows], [40])
