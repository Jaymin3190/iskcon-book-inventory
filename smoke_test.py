import json
import sys

import urllib.request

BASE = "http://127.0.0.1:8000"
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
check("6 zones exist", sum(1 for l in locs if l["type"] == "zone") == 1)

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

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)