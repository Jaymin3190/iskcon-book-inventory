import base64
import json
import os
import time
import urllib.error
import urllib.request


API_BASE = "https://api.razorpay.com/v1"


class RazorpayError(Exception):
    pass


def credentials():
    key_id = os.environ.get("RAZORPAY_KEY_ID", "").strip()
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
    return key_id, key_secret


def is_configured():
    key_id, key_secret = credentials()
    return bool(key_id and key_secret)


def create_qr_code(invoice_number, amount):
    key_id, key_secret = credentials()
    if not key_id or not key_secret:
        raise RazorpayError("Razorpay is not configured")

    payload = {
        "type": "upi_qr",
        "name": invoice_number,
        "usage": "single_use",
        "fixed_amount": True,
        "payment_amount": int(round(float(amount) * 100)),
        "description": f"Payment for invoice {invoice_number}",
        "close_by": int(time.time()) + 3600,
        "notes": {"invoice_number": invoice_number},
    }
    request = urllib.request.Request(
        f"{API_BASE}/payments/qr_codes",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Basic "
            + base64.b64encode(f"{key_id}:{key_secret}".encode("utf-8")).decode("ascii"),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("error", {}).get("description")
        except (json.JSONDecodeError, AttributeError):
            detail = None
        raise RazorpayError(detail or f"Razorpay rejected the QR request ({error.code})") from error
    except urllib.error.URLError as error:
        raise RazorpayError("Could not connect to Razorpay") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RazorpayError("Razorpay returned an invalid QR response") from error

    if not isinstance(result, dict) or not result.get("id") or not result.get("image_url"):
        raise RazorpayError("Razorpay response is missing the QR code or image URL")
    return result


def close_qr_code(qr_code_id):
    key_id, key_secret = credentials()
    if not key_id or not key_secret:
        raise RazorpayError("Razorpay is not configured")
    request = urllib.request.Request(
        f"{API_BASE}/payments/qr_codes/{qr_code_id}/close",
        data=b"{}",
        headers={
            "Authorization": "Basic "
            + base64.b64encode(f"{key_id}:{key_secret}".encode("utf-8")).decode("ascii"),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15):
            return
    except urllib.error.HTTPError as error:
        raise RazorpayError(f"Could not close the unused Razorpay QR ({error.code})") from error
    except urllib.error.URLError as error:
        raise RazorpayError("Could not connect to Razorpay to close the unused QR") from error
