from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "inventory.db"
STATIC_DIR = BASE_DIR / "static"
SECRET_KEY = "iskcon-prototype-secret-change-me"
TOKEN_EXPIRY_HOURS = 12
