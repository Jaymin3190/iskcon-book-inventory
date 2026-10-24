import json
import os
import sys

import urllib.request

BASE = os.environ.get("SMOKE_TEST_BASE", "http://127.0.0.1:8000")
passed = 0
failed = 0


def call(method, path, token=None, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS  {name}")
    else:
        failed += 1
        print(f"FAIL  {name} {extra}")


def login(username, password):
    code, data = call("POST", "/api/auth/login", body={"username": username, "password": password})
    return data.get("token") if code == 200 else None


def qty(stock_rows, book_id, loc_id):
    for s in stock_rows:
        if s["book_id"] == book_id and s["location_id"] == loc_id:
            return s["qty"]
    return None


super_t = login("superadmin", "admin123")
temple_t = login("temple-admin", "temple123")
zone_t = login("leader-temple-zone", "zone123")
dist_t = login("distributor", "dist123")

check("login super_admin", bool(super_t))
check("login temple admin", bool(temple_t))
check("login zone leader", bool(zone_t))
check("login distributor", bool(dist_t))
check("old users removed", login("store-mumbai", "store123") is None and login("zoneadmin-west", "zone123") is None)
check("bad creds rejected", login("temple-admin", "wrong") is None)

_, me = call("GET", "/api/auth/me", token=super_t)
check("me returns role", me["user"]["role"] == "super_admin")

_, books = call("GET", "/api/books", token=super_t)
check("book catalogue = 10", len(books) == 10)
_, book = call("GET", "/api/books/lookup/9780892131343", token=temple_t)
check("barcode lookup works", book["title"] == "Bhagavad-gita As It Is")
check("publication field = BBT", book["publication"] == "Bhaktivedanta Book Trust (BBT)")
check("no author/category fields", "author" not in book and "category" not in book)

_, stock = call("GET", "/api/stock", token=temple_t)
check("temple admin sees only temple", all(s["location_id"] == 1 for s in stock) and len(stock) == 10)
check("temple has 10 each", all(s["qty"] == 10 for s in stock))

_, locs = call("GET", "/api/locations", token=temple_t)
check("temple scoped locations", {l["id"] for l in locs} == {1})

_, locs = call("GET", "/api/locations", token=zone_t)
check("zone leader sees zone + temple", {l["id"] for l in locs} == {1, 2})
check("zone leader sees one assigned zone", sum(1 for l in locs if l["type"] == "zone") == 1)

_, all_locs = call("GET", "/api/locations", token=super_t)
zone_names = [l["name"] for l in all_locs if l["type"] == "zone"]
check("zones named correctly", set(zone_names) == {"Temple Zone", "Niradhar Ashram", "Gokul Nagar", "Patel Park", "Base Students", "Reliance Group"})
_, zone_stock = call("GET", "/api/stock?location_id=2", token=super_t)
check("zones start at zero", all(s["qty"] == 0 for s in zone_stock))

# --- transfer: zone leader self-service from temple ---
code, tr = call("POST", "/api/stock/transfers", token=zone_t, body={
    "from_location_id": 1, "to_location_id": 2, "note": "restock temple zone",
    "items": [{"book_id": 1, "qty": 5}, {"book_id": 5, "qty": 10}, {"book_id": 7, "qty": 4}],
})
check("zone leader requests transfer", code == 200 and tr["status"] == "requested", tr)
tid = tr["id"]

code, _ = call("POST", f"/api/stock/transfers/{tid}/approve", token=temple_t)
check("temple admin cannot approve (RBAC)", code == 403)

code, _ = call("POST", f"/api/stock/transfers/{tid}/approve", token=zone_t)
check("zone leader approves", code == 200)

code, _ = call("POST", f"/api/stock/transfers/{tid}/dispatch", token=temple_t)
check("temple admin cannot dispatch (RBAC)", code == 403)

code, _ = call("POST", f"/api/stock/transfers/{tid}/dispatch", token=zone_t)
check("zone leader dispatches", code == 200)

_, ts = call("GET", "/api/stock?location_id=1", token=super_t)
check("temple stock reduced after dispatch", qty(ts, 1, 1) == 5 and qty(ts, 5, 1) == 0 and qty(ts, 7, 1) == 6)

code, _ = call("POST", f"/api/stock/transfers/{tid}/receive", token=zone_t)
check("zone leader receives", code == 200)

_, zs = call("GET", "/api/stock?location_id=2", token=super_t)
check("zone stock increased after receive", qty(zs, 1, 2) == 5 and qty(zs, 5, 2) == 10 and qty(zs, 7, 2) == 4)

# --- POS billing at temple ---
code, inv = call("POST", "/api/pos/invoices", token=temple_t, body={
    "customer_name": "Devotee Ram", "payment_mode": "cash",
    "items": [{"barcode": "9780892132517", "qty": 2}],
})
check("temple POS invoice (2 x 450)", code == 200 and inv["total"] == 900.0, inv)
check("invoice number format", str(inv["invoice_number"]).startswith("INV-TEMPLE-"))

_, ts = call("GET", "/api/stock?location_id=1", token=super_t)
check("stock decremented on sale", qty(ts, 2, 1) == 8)

code, _ = call("POST", "/api/pos/invoices", token=temple_t, body={
    "items": [{"barcode": "9780892131343", "qty": 9999}],
})
check("oversell rejected", code == 400)

code, led = call("POST", "/api/pos/invoices", token=dist_t, body={
    "customer_name": "Temple Zone", "payment_mode": "ledger",
    "items": [{"barcode": "9780892131565", "qty": 4}],
})
check("distributor ledger billing at zone", code == 200 and led["total"] == 400.0, led)

_, inv_detail = call("GET", f"/api/pos/invoices/{inv['invoice_id']}", token=temple_t)
check("invoice detail has items", len(inv_detail["items"]) == 1)

req = urllib.request.Request(f"{BASE}/api/pos/invoices/{inv['invoice_id']}/print")
req.add_header("Authorization", "Bearer " + temple_t)
with urllib.request.urlopen(req) as resp:
    html_body = resp.read().decode()
check("printable invoice HTML", "<html" in html_body.lower() or "doctype" in html_body.lower())

# --- stock count ---
code, cnt = call("POST", "/api/stock/counts", token=temple_t, body={"location_id": 1})
check("stock count created", code == 200, cnt)
cid = cnt["count_id"]
code, _ = call("POST", f"/api/stock/counts/{cid}/items", token=temple_t,
               body=[{"book_id": 1, "counted_qty": 8}, {"book_id": 5, "counted_qty": 2}])
check("count items recorded", code == 200)
code, done = call("POST", f"/api/stock/counts/{cid}/complete", token=temple_t)
check("count complete with variances", code == 200 and done["variances"] == [3, 2], done)

# --- returns ---
code, ret = call("POST", "/api/stock/returns", token=temple_t, body={
    "location_id": 1, "reason": "unsold slow mover", "items": [{"book_id": 3, "qty": 2}],
})
check("return recorded", code == 200, ret)
rid = ret["id"]
code, _ = call("PATCH", f"/api/stock/returns/{rid}/process?credit_location_id=2", token=zone_t)
check("zone leader processes return (credits stock)", code == 200)

# --- damaged write-off ---
code, dmg = call("POST", "/api/stock/damaged", token=temple_t, body={
    "location_id": 1, "book_id": 4, "qty": 1, "reason_code": "STAINED", "note": "coffee spill",
})
check("damaged write-off", code == 200, dmg)
code, _ = call("POST", "/api/stock/damaged", token=temple_t, body={
    "location_id": 1, "book_id": 4, "qty": 1, "reason_code": "NOPE",
})
check("invalid reason code rejected", code == 400)

# --- RBAC / scope ---
code, _ = call("GET", "/api/users", token=temple_t)
check("temple admin cannot list users", code == 403)
code, _ = call("POST", "/api/books", token=temple_t, body={"title": "x", "barcode": "1"})
check("temple admin cannot create books", code == 403)
code, _ = call("GET", "/api/users", token=zone_t)
check("zone leader lists scoped users", code == 200)
code, _ = call("GET", "/api/audit", token=temple_t)
check("temple admin cannot view audit trail", code == 403)
code, _ = call("GET", "/api/audit", token=zone_t)
check("zone leader can view audit trail", code == 200)

# --- reports ---
_, sales = call("GET", "/api/reports/sales", token=super_t)
check("sales report has rows", len(sales) >= 1)
_, byb = call("GET", "/api/reports/sales-by-book", token=super_t)
check("best sellers report", len(byb) >= 1 and byb[0]["revenue"] > 0)
_, ssum = call("GET", "/api/reports/stock-summary", token=super_t)
check("stock summary lists all 7 locations", len(ssum) == 7)
_, trail = call("GET", "/api/audit", token=super_t)
check("audit trail logged actions", len(trail) >= 10)

# --- catalogue CRUD ---
code, found = call("GET", "/api/books?q=Bhagavad-gita", token=super_t)
check("book search filters by title", code == 200 and len(found) == 1 and found[0]["id"] == 1)
code, created_book = call("POST", "/api/books", token=super_t, body={
    "title": "Smoke Test Book", "barcode": "SMOKE-BOOK-001", "price": 25,
    "isbn": None, "language": None, "publication": None,
})
check("book creation", code == 200 and created_book["barcode"] == "SMOKE-BOOK-001", created_book)
test_book_id = created_book.get("id")
code, _ = call("POST", "/api/books", token=super_t, body={
    "title": "Duplicate Book", "barcode": "SMOKE-BOOK-001",
})
check("duplicate book barcode rejected", code == 409)
code, _ = call("PATCH", f"/api/books/{test_book_id}", token=super_t, body={
    "title": "Smoke Test Book Updated", "barcode": "SMOKE-BOOK-002",
    "isbn": None, "language": None, "publication": None,
})
check("book update changes title and barcode", code == 200)
code, updated_book = call("GET", f"/api/books/lookup/SMOKE-BOOK-002", token=super_t)
check("book update clears blank optional text fields", code == 200 and all(updated_book[field] is None for field in ("isbn", "language", "publication")))
code, updated = call("GET", "/api/books/lookup/SMOKE-BOOK-002", token=super_t)
check("updated book barcode is searchable", code == 200 and updated["title"] == "Smoke Test Book Updated")
code, _ = call("PATCH", f"/api/books/{test_book_id}", token=super_t, body={
    "barcode": "9780892131343",
})
check("book update rejects duplicate barcode", code == 409)
code, _ = call("DELETE", "/api/books/1", token=super_t)
check("book with stock/history cannot be deleted", code == 409)
code, _ = call("DELETE", f"/api/books/{test_book_id}", token=super_t)
check("unused book can be deleted", code == 200)

# --- location CRUD ---
code, temple_location = call("GET", "/api/locations/1", token=super_t)
check("location detail", code == 200 and temple_location["code"] == "TEMPLE")
code, created_location = call("POST", "/api/locations", token=super_t, body={
    "code": "SMOKE-LOC", "name": "Smoke Test Location", "type": "store", "parent_id": 1,
})
check("location creation", code == 200 and created_location["code"] == "SMOKE-LOC")
test_location_id = created_location.get("id")
code, store_locations = call("GET", "/api/locations?type=store", token=super_t)
check("location type filter", code == 200 and any(l["id"] == test_location_id for l in store_locations))
code, _ = call("DELETE", f"/api/locations/{test_location_id}", token=super_t)
check("unused child location can be deleted", code == 200)

# --- stock adjustment, ledger, and open-count deletion ---
code, adjusted = call("POST", "/api/stock/adjust", token=temple_t, body={
    "book_id": 1, "location_id": 1, "qty": 2, "note": "smoke adjustment",
})
check("stock adjustment adds units", code == 200 and adjusted["qty"] == 10, adjusted)
code, adjusted = call("POST", "/api/stock/adjust", token=temple_t, body={
    "book_id": 1, "location_id": 1, "qty": -1, "note": "smoke adjustment reversal",
})
check("stock adjustment removes units", code == 200 and adjusted["qty"] == 9, adjusted)
code, _ = call("POST", "/api/stock/adjust", token=temple_t, body={
    "book_id": 1, "location_id": 1, "qty": -999,
})
check("stock adjustment prevents negative inventory", code == 400)
code, ledger = call("GET", "/api/stock/ledger?location_id=1&book_id=1", token=temple_t)
check("stock ledger filters by location and book", code == 200 and len(ledger) >= 1)
code, open_count = call("POST", "/api/stock/counts", token=temple_t, body={"location_id": 1})
open_count_id = open_count.get("count_id")
code, _ = call("DELETE", f"/api/stock/counts/{open_count_id}", token=temple_t)
check("open stock count can be deleted", code == 200)
code, _ = call("DELETE", f"/api/stock/counts/{cid}", token=temple_t)
check("completed stock count cannot be deleted", code == 409)

# --- transfer read, reject, and delete ---
code, transfer_rows = call("GET", "/api/stock/transfers", token=zone_t)
check("transfer list", code == 200 and any(t["id"] == tid for t in transfer_rows))
code, transfer_detail = call("GET", f"/api/stock/transfers/{tid}", token=zone_t)
check("transfer detail includes items", code == 200 and len(transfer_detail["items"]) == 3)
code, rejected_transfer = call("POST", "/api/stock/transfers", token=zone_t, body={
    "from_location_id": 1, "to_location_id": 2, "items": [{"book_id": 1, "qty": 1}],
})
rejected_id = rejected_transfer.get("id")
code, rejected = call("POST", f"/api/stock/transfers/{rejected_id}/reject", token=zone_t)
check("requested transfer can be rejected", code == 200 and rejected["status"] == "rejected")
code, _ = call("DELETE", f"/api/stock/transfers/{rejected_id}", token=zone_t)
check("rejected transfer can be deleted", code == 200)

# --- invoice list, delete, and stock restoration ---
code, invoices = call("GET", "/api/pos/invoices", token=temple_t)
check("invoice list includes created invoice", code == 200 and any(i["id"] == inv["invoice_id"] for i in invoices))
_, before_invoice_rows = call("GET", "/api/stock?location_id=1&book_id=1", token=super_t)
before_invoice_delete = qty(before_invoice_rows, 1, 1)
code, disposable_invoice = call("POST", "/api/pos/invoices", token=temple_t, body={
    "payment_mode": "cash", "items": [{"barcode": "9780892131343", "qty": 1}],
})
disposable_invoice_id = disposable_invoice.get("invoice_id")
code, deletion = call("DELETE", f"/api/pos/invoices/{disposable_invoice_id}", token=temple_t)
check("invoice deletion restores sold stock", code == 200 and deletion["restocked_lines"] == 1)
code, restored_stock = call("GET", "/api/stock?location_id=1&book_id=1", token=super_t)
check("invoice deletion restores exact quantity", qty(restored_stock, 1, 1) == before_invoice_delete)

# --- return deletion should restore the source stock ---
before_return_delete = qty(restored_stock, 1, 1)
code, disposable_return = call("POST", "/api/stock/returns", token=temple_t, body={
    "location_id": 1, "reason": "delete-path test", "items": [{"book_id": 1, "qty": 1}],
})
disposable_return_id = disposable_return.get("id")
code, _ = call("DELETE", f"/api/stock/returns/{disposable_return_id}", token=zone_t)
check("recorded return can be deleted", code == 200)
code, after_return_delete = call("GET", "/api/stock?location_id=1&book_id=1", token=super_t)
check("deleting recorded return restores source stock", qty(after_return_delete, 1, 1) == before_return_delete)

# --- damaged record list and reversal ---
code, damaged_rows = call("GET", "/api/stock/damaged", token=temple_t)
check("damaged records list", code == 200 and len(damaged_rows) >= 1)
code, disposable_damage = call("POST", "/api/stock/damaged", token=temple_t, body={
    "location_id": 1, "book_id": 1, "qty": 1, "reason_code": "OTHER",
})
damage_id = disposable_damage.get("id")
code, damage_delete = call("DELETE", f"/api/stock/damaged/{damage_id}", token=temple_t)
check("deleting damage write-off restores stock", code == 200 and damage_delete["restocked_qty"] == 1)

# --- user CRUD ---
code, created_user = call("POST", "/api/users", token=super_t, body={
    "username": "smoke-user", "password": "initial-smoke-password", "name": "Smoke User",
    "role": "distributor", "zone_id": 2, "location_id": None,
})
check("user creation", code == 200 and created_user["username"] == "smoke-user")
test_user_id = created_user.get("id")
code, temple_user = call("POST", "/api/users", token=super_t, body={
    "username": "smoke-temple-user", "password": "temple-smoke-password", "name": "Smoke Temple User",
    "role": "temple", "zone_id": None, "location_id": 1,
})
check("temple user creation accepts null zone id", code == 200 and temple_user["username"] == "smoke-temple-user")
code, admin_user = call("POST", "/api/users", token=super_t, body={
    "username": "smoke-admin-user", "password": "admin-smoke-password", "name": "Smoke Admin User",
    "role": "super_admin", "zone_id": None, "location_id": None,
})
check("super admin creation accepts null location ids", code == 200 and admin_user["username"] == "smoke-admin-user")
code, users = call("GET", "/api/users", token=super_t)
check("user list excludes password hashes", code == 200 and "password_hash" not in str(users))
code, _ = call("PATCH", f"/api/users/{test_user_id}", token=super_t, body={
    "name": "Updated Smoke User", "password": "updated-smoke-password",
})
check("user update and password change", code == 200)
check("updated user password authenticates", bool(login("smoke-user", "updated-smoke-password")))
code, _ = call("PATCH", f"/api/users/{test_user_id}", token=super_t, body={"is_active": 0})
check("user can be disabled", code == 200 and login("smoke-user", "updated-smoke-password") is None)
code, _ = call("PATCH", f"/api/users/{test_user_id}", token=super_t, body={"is_active": 1})
check("user can be re-enabled", code == 200 and bool(login("smoke-user", "updated-smoke-password")))
code, _ = call("DELETE", f"/api/users/{test_user_id}", token=super_t)
check("user with audit history cannot be deleted", code == 409)
code, disposable_user = call("POST", "/api/users", token=super_t, body={
    "username": "smoke-delete-user", "password": "delete-smoke-password", "name": "Disposable Smoke User",
    "role": "distributor", "zone_id": 2,
})
code, _ = call("DELETE", f"/api/users/{disposable_user.get('id')}", token=super_t)
check("user without activity can be deleted", code == 200)

# --- remaining report endpoints ---
code, transfer_report = call("GET", "/api/reports/transfers", token=super_t)
check("transfer report", code == 200 and isinstance(transfer_report, list))
code, return_report = call("GET", "/api/reports/returns", token=super_t)
check("returns report", code == 200 and isinstance(return_report, list))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)