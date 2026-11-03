def location_ids(db, user) -> set:
    if user["role"] == "super_admin":
        rows = db.execute("SELECT id FROM locations").fetchall()
        return {r["id"] for r in rows}
    zone_id = user["zone_id"]
    if not zone_id:
        # Unassigned non-super users must see nothing, never everything.
        return {None}
    if user["role"] == "distributor":
        return {zone_id}
    ids = {zone_id}
    rows = db.execute("SELECT id FROM locations WHERE parent_id = ?", (zone_id,)).fetchall()
    ids.update(r["id"] for r in rows)
    row = db.execute("SELECT parent_id FROM locations WHERE id = ?", (zone_id,)).fetchone()
    if row and row["parent_id"]:
        ids.add(row["parent_id"])
    return ids


def can_view(db, user, location_id) -> bool:
    return location_id in location_ids(db, user)


def zone_location_ids(db, zone_id) -> set:
    """A location and every location beneath it, never its parent."""
    if not zone_id:
        return {None}
    rows = db.execute(
        """WITH RECURSIVE tree(id) AS (
               SELECT ?
               UNION
               SELECT l.id FROM locations l JOIN tree t ON l.parent_id = t.id
           )
           SELECT id FROM tree""",
        (zone_id,),
    ).fetchall()
    return {r["id"] for r in rows}


def invoice_location_ids(db, user) -> set:
    """Locations whose invoices a user may see.

    Invoice visibility is narrower than general location visibility: a zone user
    must not see bills at their parent warehouse, but a super admin sees them all.
    """
    if user["role"] == "super_admin":
        return location_ids(db, user)
    return zone_location_ids(db, user["zone_id"])


def zone_of(db, location_id):
    row = db.execute("SELECT parent_id FROM locations WHERE id = ?", (location_id,)).fetchone()
    return row["parent_id"] if row else None


def in_zone(db, location_id, zone_id) -> bool:
    if not zone_id:
        return False
    if location_id == zone_id:
        return True
    row = db.execute("SELECT parent_id FROM locations WHERE id = ?", (location_id,)).fetchone()
    return bool(row and row["parent_id"] == zone_id)
