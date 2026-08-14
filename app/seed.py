import sqlite3

from .auth import hash_password
from .database import DB_PATH

BOOKS = [
    ("Bhagavad-gita As It Is", "9780892131343", "9780892131343", "English", "Bhaktivedanta Book Trust (BBT)", 300),
    ("Srimad Bhagavatam (Canto 1)", "9780892132517", "9780892132517", "English", "Bhaktivedanta Book Trust (BBT)", 450),
    ("Chaitanya Charitamrita", "9780892139479", "9780892139479", "English", "Bhaktivedanta Book Trust (BBT)", 600),
    ("Krsna Book", "9780892133540", "9780892133540", "English", "Bhaktivedanta Book Trust (BBT)", 350),
    ("Sri Isopanisad", "9780892132906", "9780892132906", "English", "Bhaktivedanta Book Trust (BBT)", 120),
    ("Teachings of Lord Caitanya", "9780892133557", "9780892133557", "English", "Bhaktivedanta Book Trust (BBT)", 250),
    ("Raja-vidya", "9780892131565", "9780892131565", "English", "Bhaktivedanta Book Trust (BBT)", 100),
    ("Easy Journey to Other Planets", "9780892133201", "9780892133201", "English", "Bhaktivedanta Book Trust (BBT)", 90),
    ("Science of Self-Realization", "9780892132807", "9780892132807", "English", "Bhaktivedanta Book Trust (BBT)", 180),
    ("Coming Back", "9780892139523", "9780892139523", "English", "Bhaktivedanta Book Trust (BBT)", 150),
]

LOCATIONS = [
    ("TEMPLE", "ISKCON Temple", "warehouse", None),
    ("TZ", "Temple Zone", "zone", 1),
    ("NA", "Niradhar Ashram", "zone", 1),
    ("GN", "Gokul Nagar", "zone", 1),
    ("PP", "Patel Park", "zone", 1),
    ("BS", "Base Students", "zone", 1),
    ("RG", "Reliance Group", "zone", 1),
]

USERS = [
    ("superadmin", "admin123", "Global Super Admin", "super_admin", None, None),
    ("temple-admin", "temple123", "Temple Admin", "temple", None, 1),
    ("leader-temple-zone", "zone123", "Zone Leader - Temple Zone", "zone_admin", 2, None),
    ("leader-niradhar-ashram", "zone123", "Zone Leader - Niradhar Ashram", "zone_admin", 3, None),
    ("leader-gokul-nagar", "zone123", "Zone Leader - Gokul Nagar", "zone_admin", 4, None),
    ("leader-patel-park", "zone123", "Zone Leader - Patel Park", "zone_admin", 5, None),
    ("leader-base-students", "zone123", "Zone Leader - Base Students", "zone_admin", 6, None),
    ("leader-reliance-group", "zone123", "Zone Leader - Reliance Group", "zone_admin", 7, None),
    ("distributor", "dist123", "Distributor", "distributor", 2, None),
]


def seed():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] > 0:
        conn.close()
        return

    for code, name, ltype, parent in LOCATIONS:
        c.execute(
            "INSERT INTO locations (code, name, type, parent_id) VALUES (?,?,?,?)",
            (code, name, ltype, parent),
        )

    for title, isbn, barcode, lang, publication, price in BOOKS:
        c.execute(
            "INSERT INTO books (title, isbn, barcode, language, publication, price) VALUES (?,?,?,?,?,?)",
            (title, isbn, barcode, lang, publication, price),
        )

    for username, pwd, name, role, zone_id, loc_id in USERS:
        c.execute(
            "INSERT INTO users (username, password_hash, name, role, zone_id, location_id) VALUES (?,?,?,?,?,?)",
            (username, hash_password(pwd), name, role, zone_id, loc_id),
        )

    for book_id in range(1, len(BOOKS) + 1):
        c.execute(
            "INSERT INTO stock (book_id, location_id, qty) VALUES (?, 1, 10)",
            (book_id,),
        )
        for zone_id in range(2, len(LOCATIONS) + 1):
            c.execute(
                "INSERT INTO stock (book_id, location_id, qty) VALUES (?, ?, 0)",
                (book_id, zone_id),
            )

    conn.commit()
    conn.close()