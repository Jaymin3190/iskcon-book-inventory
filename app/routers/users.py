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
    is_active: int = 1


class UserPatch(BaseModel):
    username: Optional[str] = None
    name: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    zone_id: Optional[int] = None
    is_active: Optional[int] = None


def sanitize(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "name": row["name"],
        "role": row["role"],
        "zone_id": row["zone_id"],
        "zone_name": row["zone_name"],
        "is_active": row["is_active"],
        "created_at": row["created_at"],
    }


@router.get("")
def list_users(db=Depends(get_db), user=Depends(require_roles("super_admin", "zone_admin"))):
    base = """SELECT u.*, z.name AS zone_name
              FROM users u
              LEFT JOIN locations z ON u.zone_id = z.id"""
    if user["role"] == "super_admin":
        rows = db.execute(base + " ORDER BY u.id").fetchall()
    else:
        rows = db.execute(base + " WHERE u.zone_id = ? ORDER BY u.id", (user["zone_id"],)).fetchall()
    return [sanitize(r) for r in rows]


def clean_username(value: str) -> str:
    username = value.strip()
    if not username:
        raise HTTPException(400, "Username cannot be empty")
    if any(ch.isspace() for ch in username):
        raise HTTPException(400, "Username cannot contain spaces")
    return username


def validate_zone(db, zone_id: Optional[int]) -> None:
    if not zone_id or not db.execute(
        "SELECT id FROM locations WHERE id = ? AND type = 'zone'", (zone_id,)
    ).fetchone():
        raise HTTPException(400, "A valid zone is required for every role except super admin")


@router.post("")
def create_user(data: UserIn, db=Depends(get_db), user=Depends(require_roles("super_admin"))):
    if data.role not in VALID_ROLES:
        raise HTTPException(400, "Invalid role")
    if not data.password:
        raise HTTPException(400, "Password is required")
    if not data.name.strip():
        raise HTTPException(400, "Name is required")
    username = clean_username(data.username)
    if data.role != "super_admin" and not data.zone_id:
        raise HTTPException(400, "A zone is required for every role except super admin")
    if data.role != "super_admin":
        validate_zone(db, data.zone_id)
    existing = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        raise HTTPException(409, "Username already exists")
    cur = db.execute(
        """INSERT INTO users (username, password_hash, name, role, zone_id, is_active)
           VALUES (?,?,?,?,?,?)""",
        (username, hash_password(data.password), data.name, data.role, data.zone_id, data.is_active),
    )
    db.commit()
    log_audit(db, user["id"], "USER_CREATE", "user", cur.lastrowid, username, user["username"])
    return {"id": cur.lastrowid, "username": username}


@router.patch("/{user_id}")
def update_user(user_id: int, data: UserPatch, db=Depends(get_db), user=Depends(require_roles("super_admin"))):
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "User not found")
    provided = data.model_fields_set
    fields = []
    values = []
    if data.username is not None:
        new_username = clean_username(data.username)
        if new_username != row["username"]:
            duplicate = db.execute(
                "SELECT id FROM users WHERE username = ? AND id != ?", (new_username, user_id)
            ).fetchone()
            if duplicate:
                raise HTTPException(409, "Username already exists")
        fields.append("username = ?")
        values.append(new_username)
    if data.name is not None:
        if not data.name.strip():
            raise HTTPException(400, "Name cannot be empty")
        fields.append("name = ?")
        values.append(data.name.strip())
    if data.role is not None:
        if data.role not in VALID_ROLES:
            raise HTTPException(400, "Invalid role")
        if row["role"] == "super_admin" and data.role != "super_admin" and row["is_active"]:
            others = db.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = 'super_admin' AND is_active = 1 AND id != ?",
                (user_id,),
            ).fetchone()["n"]
            if others == 0:
                raise HTTPException(400, "Cannot change the role of the last active super admin")
        fields.append("role = ?")
        values.append(data.role)
    if data.password is not None:
        if not data.password:
            raise HTTPException(400, "Password cannot be empty")
        fields.append("password_hash = ?")
        values.append(hash_password(data.password))
    # Explicit null is meaningful here: demoting someone to super admin has to
    # clear the zone that role no longer needs.
    if "zone_id" in provided:
        fields.append("zone_id = ?")
        values.append(data.zone_id)
    if data.is_active is not None:
        if row["role"] == "super_admin" and not data.is_active and row["is_active"]:
            others = db.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = 'super_admin' AND is_active = 1 AND id != ?",
                (user_id,),
            ).fetchone()["n"]
            if others == 0:
                raise HTTPException(400, "Cannot disable the last active super admin")
        fields.append("is_active = ?")
        values.append(data.is_active)

    final_role = data.role if data.role is not None else row["role"]
    final_zone = data.zone_id if "zone_id" in provided else row["zone_id"]
    if final_role != "super_admin" and not final_zone:
        raise HTTPException(400, "A zone is required for every role except super admin")
    if final_role != "super_admin":
        validate_zone(db, final_zone)

    if fields:
        values.append(user_id)
        db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
        db.commit()
    renamed = data.username is not None and data.username.strip() != row["username"]
    details = row["username"] + (f" -> {data.username.strip()}" if renamed else "")
    if data.password:
        details += " (password reset)"
    log_audit(db, user["id"], "USER_UPDATE", "user", user_id, details, user["username"])
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
    ]
    for table, col in refs:
        r = db.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {col} = ?", (user_id,)).fetchone()
        if r["n"]:
            raise HTTPException(409, f"Cannot delete: user has activity in {table.replace('_', ' ')}")
    db.execute("DELETE FROM audit_logs WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    log_audit(db, user["id"], "USER_DELETE", "user", user_id, target["username"], user["username"])
    return {"ok": True}
