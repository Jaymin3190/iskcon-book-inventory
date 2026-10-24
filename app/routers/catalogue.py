from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_roles
from ..database import get_db
from ..services.audit import log_audit

router = APIRouter(prefix="/api/books", tags=["books"])


class BookIn(BaseModel):
    title: str
    isbn: Optional[str] = None
    barcode: str
    language: Optional[str] = None
    publication: Optional[str] = None
    price: float = 0


class BookPatch(BaseModel):
    title: Optional[str] = None
    isbn: Optional[str] = None
    barcode: Optional[str] = None
    language: Optional[str] = None
    publication: Optional[str] = None
    price: Optional[float] = None


@router.get("")
def list_books(q: str = None, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    sql = "SELECT * FROM books"
    params = []
    if q:
        sql += " WHERE title LIKE ? OR isbn LIKE ? OR barcode LIKE ?"
        like = f"%{q}%"
        params = [like, like, like]
    sql += " ORDER BY title"
    return [dict(r) for r in db.execute(sql, params).fetchall()]


@router.get("/lookup/{barcode}")
def lookup_barcode(barcode: str, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    book = db.execute(
        "SELECT * FROM books WHERE barcode = ? OR isbn = ?",
        (barcode, barcode),
    ).fetchone()
    if not book:
        raise HTTPException(404, "No book found for this barcode/ISBN")
    return dict(book)


@router.post("")
def create_book(data: BookIn, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    dup = db.execute("SELECT id FROM books WHERE barcode = ?", (data.barcode,)).fetchone()
    if dup:
        raise HTTPException(409, "A book with this barcode already exists")
    cur = db.execute(
        """INSERT INTO books (title, isbn, barcode, language, publication, price)
           VALUES (?,?,?,?,?,?)""",
        (data.title, data.isbn, data.barcode, data.language, data.publication, data.price),
    )
    db.commit()
    log_audit(db, user["id"], "BOOK_CREATE", "book", cur.lastrowid, data.title, user["username"])
    return {"id": cur.lastrowid, "barcode": data.barcode}


@router.patch("/{book_id}")
def update_book(book_id: int, data: BookPatch, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    row = db.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Book not found")
    if data.barcode is not None:
        dup = db.execute("SELECT id FROM books WHERE barcode = ? AND id != ?", (data.barcode, book_id)).fetchone()
        if dup:
            raise HTTPException(409, "A book with this barcode already exists")
    fields, values = [], []
    for key in ("title", "isbn", "barcode", "language", "publication", "price"):
        if key not in data.model_fields_set:
            continue
        val = getattr(data, key)
        if val is None and key in ("title", "barcode", "price"):
            raise HTTPException(400, f"{key} cannot be null")
        fields.append(f"{key} = ?")
        values.append(val)
    if fields:
        values.append(book_id)
        db.execute(f"UPDATE books SET {', '.join(fields)} WHERE id = ?", values)
        db.commit()
    log_audit(db, user["id"], "BOOK_UPDATE", "book", book_id, row["title"], user["username"])
    return {"ok": True}


@router.delete("/{book_id}")
def delete_book(book_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    book = db.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if not book:
        raise HTTPException(404, "Book not found")
    row = db.execute("SELECT COALESCE(SUM(qty), 0) AS total FROM stock WHERE book_id = ?", (book_id,)).fetchone()
    if row["total"] > 0:
        raise HTTPException(409, "Cannot delete: stock still exists for this book")
    for table in ("stock_movements", "invoice_items", "transfer_items", "return_items", "damaged", "stock_count_items"):
        r = db.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE book_id = ?", (book_id,)).fetchone()
        if r["n"]:
            raise HTTPException(409, f"Cannot delete: book is referenced in {table.replace('_', ' ')}")
    db.execute("DELETE FROM stock WHERE book_id = ?", (book_id,))
    db.execute("DELETE FROM books WHERE id = ?", (book_id,))
    db.commit()
    log_audit(db, user["id"], "BOOK_DELETE", "book", book_id, book["title"], user["username"])
    return {"ok": True}
