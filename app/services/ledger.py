def ensure_stock(db, book_id: int, location_id: int) -> int:
    db.execute(
        "INSERT OR IGNORE INTO stock (book_id, location_id, qty) VALUES (?, ?, 0)",
        (book_id, location_id),
    )
    row = db.execute(
        "SELECT qty FROM stock WHERE book_id = ? AND location_id = ?",
        (book_id, location_id),
    ).fetchone()
    return row["qty"]


def move_stock(db, book_id, location_id, delta, change_type, ref_type, ref_id, user_id, note=None):
    current = ensure_stock(db, book_id, location_id)
    new_qty = current + delta
    if new_qty < 0:
        raise ValueError(f"insufficient stock for book {book_id} at location {location_id}")
    db.execute(
        "UPDATE stock SET qty = ?, updated_at = datetime('now') WHERE book_id = ? AND location_id = ?",
        (new_qty, book_id, location_id),
    )
    db.execute(
        """INSERT INTO stock_movements (book_id, location_id, qty, change_type, ref_type, ref_id, user_id, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (book_id, location_id, delta, change_type, ref_type, ref_id, user_id, note),
    )
    db.commit()


def check_available(db, book_id, location_id, qty) -> bool:
    return ensure_stock(db, book_id, location_id) >= qty
