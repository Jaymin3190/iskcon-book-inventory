import unittest

from app.services.invoice_pdf import build_invoice_pdf


class InvoicePdfTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
