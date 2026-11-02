from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_roles
from ..database import get_db
from ..services.audit import log_audit
from ..services.scope import location_ids

router = APIRouter(prefix="/api/locations", tags=["locations"])


class LocationIn(BaseModel):
    code: str
    name: str
    type: str
    parent_id: Optional[int] = None


class LocationPatch(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    type: Optional[str] = None
    parent_id: Optional[int] = None


@router.get("")
def list_locations(type: str = None, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    ids = location_ids(db, user)
    sql = "SELECT l.*, p.name AS parent_name FROM locations l LEFT JOIN locations p ON l.parent_id = p.id WHERE l.id IN ({})".format(
        ",".join("?" * len(ids))
    )
    params = list(ids)
    if type:
        sql += " AND l.type = ?"
        params.append(type)
    sql += " ORDER BY l.type, l.name"
    return [dict(r) for r in db.execute(sql, params).fetchall()]


@router.get("/{location_id}")
def get_location(location_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin", "distributor", "temple"))):
    if location_id not in location_ids(db, user):
        raise HTTPException(403, "Forbidden")
    row = db.execute(
        "SELECT l.*, p.name AS parent_name FROM locations l LEFT JOIN locations p ON l.parent_id = p.id WHERE l.id = ?",
        (location_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Location not found")
    return dict(row)


@router.post("")
def create_location(data: LocationIn, db=Depends(get_db), user=Depends(require_roles("super_admin"))):
    if data.type not in ("warehouse", "zone", "store"):
        raise HTTPException(400, "Invalid location type")
    dup = db.execute("SELECT id FROM locations WHERE code = ?", (data.code,)).fetchone()
    if dup:
        raise HTTPException(409, "Location code already exists")
    cur = db.execute(
        "INSERT INTO locations (code, name, type, parent_id) VALUES (?,?,?,?)",
        (data.code, data.name, data.type, data.parent_id),
    )
    db.commit()
    log_audit(db, user["id"], "LOCATION_CREATE", "location", cur.lastrowid, data.name, user["username"])
    return {"id": cur.lastrowid, "code": data.code}


@router.patch("/{location_id}")
def update_location(location_id: int, data: LocationPatch, db=Depends(get_db), user=Depends(require_roles("super_admin"))):
    row = db.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Location not found")

    changes = data.model_dump(exclude_unset=True)
    if not changes:
        return {"ok": True}

    code = (changes.get("code", row["code"]) or "").strip()
    name = (changes.get("name", row["name"]) or "").strip()
    new_type = changes.get("type", row["type"])
    parent_id = changes.get("parent_id", row["parent_id"])

    if not code:
        raise HTTPException(400, "Location code is required")
    if not name:
        raise HTTPException(400, "Location name is required")
    if new_type not in ("warehouse", "zone", "store"):
        raise HTTPException(400, "Invalid location type")

    dup = db.execute("SELECT id FROM locations WHERE code = ? AND id != ?", (code, location_id)).fetchone()
    if dup:
        raise HTTPException(409, "Location code already exists")

    if parent_id is not None:
        if parent_id == location_id:
            raise HTTPException(400, "A location cannot be its own parent")
        parent = db.execute("SELECT id, parent_id FROM locations WHERE id = ?", (parent_id,)).fetchone()
        if not parent:
            raise HTTPException(400, "Parent location not found")
        seen = {location_id}
        current = parent
        while current is not None:
            if current["id"] in seen:
                raise HTTPException(400, "Parent selection would create a loop")
            seen.add(current["id"])
            current = db.execute(
                "SELECT id, parent_id FROM locations WHERE id = ?", (current["parent_id"],)
            ).fetchone() if current["parent_id"] is not None else None

    db.execute(
        "UPDATE locations SET code = ?, name = ?, type = ?, parent_id = ? WHERE id = ?",
        (code, name, new_type, parent_id, location_id),
    )
    db.commit()
    log_audit(
        db, user["id"], "LOCATION_UPDATE", "location", location_id,
        f"{row['code']} / {row['name']} -> {code} / {name} ({new_type})",
        user["username"],
    )
    return {"ok": True}


@router.delete("/{location_id}")
def delete_location(location_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin"))):
    row = db.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Location not found")
    children = db.execute("SELECT COUNT(*) AS n FROM locations WHERE parent_id = ?", (location_id,)).fetchone()["n"]
    if children:
        raise HTTPException(409, "Cannot delete: location has child locations")
    row2 = db.execute("SELECT COALESCE(SUM(qty), 0) AS total FROM stock WHERE location_id = ?", (location_id,)).fetchone()
    if row2["total"] > 0:
        raise HTTPException(409, "Cannot delete: stock still exists at this location")
    refs = [
        ("transfers", "from_location_id"),
        ("transfers", "to_location_id"),
        ("invoices", "location_id"),
        ("returns", "location_id"),
        ("returns", "credit_location_id"),
        ("damaged", "location_id"),
        ("stock_movements", "location_id"),
        ("stock_counts", "location_id"),
    ]
    for table, col in refs:
        r = db.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {col} = ?", (location_id,)).fetchone()
        if r["n"]:
            raise HTTPException(409, f"Cannot delete: location is referenced in {table.replace('_', ' ')}")
    assigned = db.execute(
        "SELECT COUNT(*) AS n FROM users WHERE zone_id = ?",
        (location_id,),
    ).fetchone()["n"]
    if assigned:
        raise HTTPException(409, "Cannot delete: users are assigned to this location")
    db.execute("DELETE FROM stock WHERE location_id = ?", (location_id,))
    db.execute("DELETE FROM locations WHERE id = ?", (location_id,))
    db.commit()
    log_audit(db, user["id"], "LOCATION_DELETE", "location", location_id, row["name"], user["username"])
    return {"ok": True}
