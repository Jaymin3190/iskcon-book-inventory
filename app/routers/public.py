"""Public, token-based invoice pages.

Customers receive a link containing an unguessable share token. Opening it
shows a read-only invoice with a PDF download button. No login is required,
and only the single invoice the token belongs to is exposed.
"""

import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response

from ..database import get_db
from ..services.invoice_pdf import build_invoice_pdf
from .pos import invoice_html

router = APIRouter(tags=["public"])


def _shared_invoice(token: str, db):
    if not token:
        raise HTTPException(404, "Invoice not found")
    inv = db.execute(
        """SELECT i.*, l.code AS location_code, l.name AS location_name,
                  u.name AS cashier_name, d.name AS distributor_name
           FROM invoices i
           JOIN locations l ON l.id = i.location_id
           LEFT JOIN users u ON u.id = i.cashier_id
           LEFT JOIN users d ON d.id = i.distributor_id
           WHERE i.share_token = ?""",
        (token,),
    ).fetchone()
    if not inv or inv["status"] != "paid":
        raise HTTPException(404, "Invoice not found")
    items = db.execute(
        """SELECT ii.*, b.title, b.barcode
           FROM invoice_items ii JOIN books b ON b.id = ii.book_id
           WHERE ii.invoice_id = ?""",
        (inv["id"],),
    ).fetchall()
    return inv, items


@router.get("/i/{token}", response_class=HTMLResponse)
def shared_invoice_page(token: str, db=Depends(get_db)):
    inv, items = _shared_invoice(token, db)
    return invoice_html(inv, items, pdf_url=f"/i/{token}/pdf")


@router.get("/i/{token}/pdf")
def shared_invoice_pdf(token: str, db=Depends(get_db)):
    inv, items = _shared_invoice(token, db)
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", inv["invoice_number"])
    return Response(
        content=build_invoice_pdf(inv, items),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}.pdf"'},
    )
