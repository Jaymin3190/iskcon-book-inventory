def location_ids(db, user) -> set:
    role = user["role"]
    if role == "super_admin":
        rows = db.execute("SELECT id FROM locations").fetchall()
        return {r["id"] for r in rows}
    if role in ("zone_admin", "distributor"):
        ids = {user["zone_id"]}
        rows = db.execute("SELECT id FROM locations WHERE parent_id = ?", (user["zone_id"],)).fetchall()
        ids.update(r["id"] for r in rows)
        row = db.execute("SELECT parent_id FROM locations WHERE id = ?", (user["zone_id"],)).fetchone()
        if row and row["parent_id"]:
            ids.add(row["parent_id"])
        return ids
    ids = {user["location_id"]}
    if user["location_id"]:
        row = db.execute("SELECT parent_id FROM locations WHERE id = ?", (user["location_id"],)).fetchone()
        if row and row["parent_id"]:
            ids.add(row["parent_id"])
    return ids


def can_view(db, user, location_id) -> bool:
    return location_id in location_ids(db, user)


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
