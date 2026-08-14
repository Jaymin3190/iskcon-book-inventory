import base64
import hashlib
import hmac
import json
import secrets
import time

from fastapi import Depends, HTTPException, Request

from .config import SECRET_KEY, TOKEN_EXPIRY_HOURS
from .database import get_db

PBKDF2_ITERATIONS = 100000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()
    return f"{salt}${dk}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, dk = stored.split("$")
    except ValueError:
        return False
    d = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()
    return hmac.compare_digest(d, dk)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64url(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def create_token(user_id: int, username: str, role: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({
        "sub": user_id,
        "username": username,
        "role": role,
        "exp": int(time.time()) + TOKEN_EXPIRY_HOURS * 3600,
    }).encode())
    body = f"{header}.{payload}"
    sig = _b64url(hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def decode_token(token: str):
    try:
        header, payload, sig = token.split(".")
        body = f"{header}.{payload}"
        expected = _b64url(hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(_unb64url(payload))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


def get_current_user(request: Request, db=Depends(get_db)):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    data = decode_token(auth[7:])
    if not data:
        raise HTTPException(401, "Invalid or expired token")
    user = db.execute(
        """SELECT u.*, l.name AS location_name, z.name AS zone_name
           FROM users u
           LEFT JOIN locations l ON u.location_id = l.id
           LEFT JOIN locations z ON u.zone_id = z.id
           WHERE u.id = ?""",
        (data["sub"],),
    ).fetchone()
    if not user or not user["is_active"]:
        raise HTTPException(401, "User not found or inactive")
    return user


def require_roles(*roles):
    def dep(user=Depends(get_current_user)):
        if user["role"] not in roles:
            raise HTTPException(403, "Forbidden")
        return user
    return dep


def public_user(user) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "name": user["name"],
        "role": user["role"],
        "zone_id": user["zone_id"],
        "zone_name": user["zone_name"],
        "location_id": user["location_id"],
        "location_name": user["location_name"],
        "is_active": user["is_active"],
    }
