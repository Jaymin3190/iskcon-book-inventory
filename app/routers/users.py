from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import hash_password, require_roles
from ..database import get_db
from ..services.audit import log_audit

router = APIRouter(prefix="/api/users", tags=["users"])

VALID_ROLES = {"super_admin", "zone_admin", "distributor", "temple"}


class UserIn(BaseModel):
    username: str
    password: str
    name: str
    role: str
    zone_id: Optional[int] = None
    location_id: Optional[int] = None
    is_active: int = 1


class UserPatch(BaseModel):
    name: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    zone_id: Optional[int] = None
    location_id: Optional[int] = None
    is_active: Optional[int] = None


def sanitize(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "name": row["name"],
        "role": row["role"],
        "zone_id": row["zone_id"],
        "zone_name": row["zone_name"],
        "location_id": row["location_id"],
        "location_name": row["location_name"],
        "is_active": row["is_active"],
        "created_at": row["created_at"],
    }


@router.get("")
def list_users(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    base = """SELECT u.*, l.name AS location_name, z.name AS zone_name
              FROM users u
              LEFT JOIN locations l ON u.location_id = l.id
              LEFT JOIN locations z ON u.zone_id = z.id"""
    if user["role"] == "super_admin":
        rows = db.execute(base + " ORDER BY u.id").fetchall()
    else:
        rows = db.execute(base + " WHERE u.zone_id = ? ORDER BY u.id", (user["zone_id"],)).fetchall()
    return [sanitize(r) for r in rows]


@router.post("")
def create_user(data: UserIn, db=Depends(get_db), user=Depends(require_roles("super_admin"))):
    if data.role not in VALID_ROLES:
        raise HTTPException(400, "Invalid role")
    if data.role == "temple" and not data.location_id:
        raise HTTPException(400, "location_id is required for temple users")
    if data.role in ("zone_admin", "distributor") and not data.zone_id:
        raise HTTPException(400, "zone_id is required for zone_admin/distributor users")
    existing = db.execute("SELECT id FROM users WHERE username = ?", (data.username,)).fetchone()
    if existing:
        raise HTTPException(409, "Username already exists")
    cur = db.execute(
        """INSERT INTO users (username, password_hash, name, role, zone_id, location_id, is_active)
           VALUES (?,?,?,?,?,?,?)""",
        (data.username, hash_password(data.password), data.name, data.role, data.zone_id, data.location_id, data.is_active),
    )
    db.commit()
    log_audit(db, user["id"], "USER_CREATE", "user", cur.lastrowid, data.username, user["username"])
    return {"id": cur.lastrowid, "username": data.username}


@router.patch("/{user_id}")
def update_user(user_id: int, data: UserPatch, db=Depends(get_db), user=Depends(require_roles("super_admin"))):
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "User not found")
    fields = []
    values = []
    if data.name is not None:
        fields.append("name = ?")
        values.append(data.name)
    if data.role is not None:
        if data.role not in VALID_ROLES:
            raise HTTPException(400, "Invalid role")
        fields.append("role = ?")
        values.append(data.role)
    if data.password is not None:
        fields.append("password_hash = ?")
        values.append(hash_password(data.password))
    if data.zone_id is not None:
        fields.append("zone_id = ?")
        values.append(data.zone_id)
    if data.location_id is not None:
        fields.append("location_id = ?")
        values.append(data.location_id)
    if data.is_active is not None:
        fields.append("is_active = ?")
        values.append(data.is_active)
    if fields:
        values.append(user_id)
        db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
        db.commit()
    log_audit(db, user["id"], "USER_UPDATE", "user", user_id, row["username"], user["username"])
    return {"ok": True}


@router.delete("/{user_id}")
def delete_user(user_id: int, db=Depends(get_db), user=Depends(require_roles("super_admin"))):
    if user_id == user["id"]:
        raise HTTPException(400, "Cannot delete your own account")
    target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        raise HTTPException(404, "User not found")
    if target["role"] == "super_admin":
        n = db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'super_admin' AND is_active = 1"
        ).fetchone()["n"]
        if n <= 1:
            raise HTTPException(400, "Cannot delete the last active super admin")
    refs = [
        ("stock_movements", "user_id"),
        ("invoices", "cashier_id"),
        ("transfers", "requested_by"),
        ("transfers", "approved_by"),
        ("transfers", "dispatched_by"),
        ("transfers", "received_by"),
        ("returns", "created_by"),
        ("returns", "processed_by"),
        ("damaged", "created_by"),
        ("stock_counts", "counted_by"),
        ("audit_logs", "user_id"),
    ]
    for table, col in refs:
        r = db.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {col} = ?", (user_id,)).fetchone()
        if r["n"]:
            raise HTTPException(409, f"Cannot delete: user has activity in {table.replace('_', ' ')}")
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    log_audit(db, user["id"], "USER_DELETE", "user", user_id, target["username"], user["username"])
    return {"ok": True}
