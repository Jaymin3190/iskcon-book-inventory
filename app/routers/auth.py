from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import create_token, get_current_user, public_user, verify_password
from ..database import get_db
from ..services.audit import log_audit

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(data: LoginIn, db=Depends(get_db)):
    user = db.execute(
        """SELECT u.*, l.name AS location_name, z.name AS zone_name
           FROM users u
           LEFT JOIN locations l ON u.location_id = l.id
           LEFT JOIN locations z ON u.zone_id = z.id
           WHERE u.username = ? AND u.is_active = 1""",
        (data.username,),
    ).fetchone()
    if not user or not verify_password(data.password, user["password_hash"]):
        raise HTTPException(401, "Invalid username or password")
    token = create_token(user["id"], user["username"], user["role"])
    log_audit(db, user["id"], "LOGIN", "user", user["id"], data.username, user["username"])
    return {"token": token, "user": public_user(user)}


@router.get("/me")
def me(user=Depends(get_current_user)):
    return {"user": public_user(user)}
