import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(
    os.environ.get("DATABASE_PATH", str(BASE_DIR / "data" / "inventory.db"))
).expanduser()
STATIC_DIR = BASE_DIR / "static"
SECRET_KEY = os.environ.get("SECRET_KEY", "iskcon-prototype-secret-change-me")
TOKEN_EXPIRY_HOURS = 12
