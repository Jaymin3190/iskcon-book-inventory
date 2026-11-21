import sqlite3
import unittest
from unittest.mock import patch

from app.database import _migrate_invoice_location
from app.services import geocoding
from app.services.invoice_pdf import build_invoice_pdf
from app.routers.pos import invoice_html


class InvoicePdfTests(unittest.TestCase):
    def test_invoice_location_migration_adds_coordinates_to_existing_tables(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY)")

        _migrate_invoice_location(connection)

        columns = {row[1] for row in connection.execute("PRAGMA table_info(invoices)")}
        self.assertIn("latitude", columns)
        self.assertIn("longitude", columns)
        self.assertIn("area_name", columns)
        connection.close()

    def test_reverse_geocoding_returns_readable_area_and_uses_cache(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"address":{"neighbourhood":"Gokul Nagar","city":"Jamnagar",'
            b'"state_district":"Jamnagar","state":"Gujarat"}}'
        )
        cache_key = (35.12345, 70.12345)
        geocoding._area_cache.pop(cache_key, None)

        with patch.object(geocoding.urllib.request, "urlopen", return_value=response) as urlopen, \
             patch.object(geocoding.time, "monotonic", return_value=100):
            area = geocoding.reverse_geocode(*cache_key)
            cached_area = geocoding.reverse_geocode(*cache_key)

        self.assertEqual(area, "Gokul Nagar, Jamnagar, Gujarat")
        self.assertEqual(cached_area, area)
        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), "iskcon-book-inventory/1.0")

    def test_builds_a_pdf_receipt_with_ist_invoice_details(self):
        invoice = {
            "invoice_number": "INV-cashier-0001",
            "customer_name": "A & B",
            "customer_phone": "9999999999",
            "created_at": "2026-11-03 09:30:00",
            "cashier_name": "Cashier",
            "payment_mode": "cash",
            "payment_ref": None,
            "razorpay_qr_code_id": None,
            "subtotal": 150.25,
            "discount": 10.25,
            "total": 140,
        }
        items = [{
            "title": "Book <One>",
            "qty": 1,
            "unit_price": 150.25,
            "line_total": 150.25,
        }]

        result = build_invoice_pdf(invoice, items)

        self.assertTrue(result.startswith(b"%PDF-"))
        self.assertIn(b"%%EOF", result)

    def test_printable_invoice_omits_balance_due(self):
        invoice = {
            "invoice_number": "INV-cashier-0002",
            "customer_name": "Customer",
            "customer_phone": "9999999999",
            "created_at": "2026-11-03 09:30:00",
            "cashier_name": "Cashier",
            "distributor_name": "Distributor",
            "payment_mode": "cash",
            "payment_ref": None,
            "razorpay_qr_code_id": None,
            "discount": 0,
            "total": 100,
            "amount_paid": 25,
        }
        items = [{
            "title": "Book",
            "qty": 1,
            "unit_price": 100,
            "line_total": 100,
        }]

        result = invoice_html(invoice, items)

        self.assertIn("Amount paid:", result)
        self.assertNotIn("Balance due", result)
        self.assertNotIn("Amount due", result)


if __name__ == "__main__":
    unittest.main()
