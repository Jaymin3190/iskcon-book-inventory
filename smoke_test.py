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
temple_t = login("templeAdmin", "temple123")
zone_t = login("leader-temple-zone", "zone123")
dist_t = login("distributor", "dist123")

check("login super_admin", bool(super_t))
check("login temple admin", bool(temple_t))
check("login zone leader", bool(zone_t))
check("login distributor", bool(dist_t))
check("old users removed", login("store-mumbai", "store123") is None and login("zoneadmin-west", "zone123") is None)
check("bad creds rejected", login("templeAdmin", "wrong") is None)

_, me = call("GET", "/api/auth/me", token=super_t)
check("me returns role", me["user"]["role"] == "super_admin")

_, books = call("GET", "/api/books", token=super_t)
check("book catalogue = 10", len(books) == 10)
_, book = call("GET", "/api/books/lookup/9780892131343", token=temple_t)
check("barcode lookup works", book["title"] == "Bhagavad-gita As It Is")
check("publication field = BBT", book["publication"] == "Bhaktivedanta Book Trust (BBT)")
check("no author/category fields", "author" not in book and "category" not in book)

_, stock = call("GET", "/api/stock", token=temple_t)
check("temple admin sees warehouse + its zone",
      {s["location_id"] for s in stock} == {1, 2} and len(stock) == 20)
check("temple has 10 each at the warehouse",
      all(s["qty"] == 10 for s in stock if s["location_id"] == 1))

_, locs = call("GET", "/api/locations", token=temple_t)
check("temple scoped locations", {l["id"] for l in locs} == {1, 2})

_, locs = call("GET", "/api/locations", token=zone_t)
check("zone leader sees zone + temple", {l["id"] for l in locs} == {1, 2})
check("zone leader sees one assigned zone", sum(1 for l in locs if l["type"] == "zone") == 1)

_, all_locs = call("GET", "/api/locations", token=super_t)
zone_names = [l["name"] for l in all_locs if l["type"] == "zone"]
check("zones named correctly", set(zone_names) == {"templeZone", "Niradhar Ashram", "Gokul Nagar", "Patel Park", "Base Students", "Reliance Group"})
_, zone_stock = call("GET", "/api/stock?location_id=2", token=super_t)
check("zones start at zero", all(s["qty"] == 0 for s in zone_stock))
_, stock_summary = call("GET", "/api/reports/stock-summary", token=super_t)
check("super admin can view total stock across locations", sum(s["total_units"] for s in stock_summary) == 100)
check("stock summary identifies warehouse availability", sum(
    s["total_units"] for s in stock_summary if s["type"] == "warehouse" and s["parent_id"] is None
) == 100)

code, admin_invoice = call("POST", "/api/pos/invoices", token=super_t, body={
    "customer_name": "Walk-in", "customer_phone": "9999999999",
    "payment_mode": "cash", "items": [{"barcode": "9780892139523", "qty": 1}],
})
check("super admin can bill without an assigned location", code == 200 and admin_invoice["invoice_number"].startswith("INV-superadmin-"), admin_invoice)

# --- transfer: zone leader self-service from temple ---
code, tr = call("POST", "/api/stock/transfers", token=zone_t, body={
    "from_location_id": 1, "to_location_id": 2, "note": None,
    "items": [{"book_id": 1, "qty": 5}, {"book_id": 2, "qty": 10},
              {"book_id": 5, "qty": 10}, {"book_id": 7, "qty": 4}],
})
check("zone leader requests transfer", code == 200 and tr["status"] == "requested", tr)
tid = tr["id"]

code, _ = call("POST", f"/api/stock/transfers/{tid}/approve", token=temple_t)
check("temple user cannot approve transfers", code == 403)

code, _ = call("POST", f"/api/stock/transfers/{tid}/approve", token=zone_t)
check("zone admin approves transfer", code == 200)

code, _ = call("POST", f"/api/stock/transfers/{tid}/dispatch", token=temple_t)
check("temple user cannot dispatch transfers", code == 403)

code, _ = call("POST", f"/api/stock/transfers/{tid}/dispatch", token=zone_t)
check("zone admin dispatches transfer", code == 200)

_, ts = call("GET", "/api/stock?location_id=1", token=super_t)
check("temple stock reduced after dispatch", qty(ts, 1, 1) == 5 and qty(ts, 5, 1) == 0 and qty(ts, 7, 1) == 6)

code, _ = call("POST", f"/api/stock/transfers/{tid}/receive", token=zone_t)
check("zone leader receives", code == 200)

_, zs = call("GET", "/api/stock?location_id=2", token=super_t)
check("zone stock increased after receive", qty(zs, 1, 2) == 5 and qty(zs, 2, 2) == 10
      and qty(zs, 5, 2) == 10 and qty(zs, 7, 2) == 4)

_, zone_only_stock = call("GET", "/api/stock", token=zone_t)
check("zone admin sees own zone stock only",
      {row["location_id"] for row in zone_only_stock} == {2} and bool(zone_only_stock), zone_only_stock)
_, zone_warehouse_stock = call("GET", "/api/stock?location_id=1", token=zone_t)
check("zone admin cannot view parent warehouse stock", not zone_warehouse_stock)
code, zone_ledger = call("GET", "/api/stock/ledger", token=zone_t)
check("zone admin ledger is limited to own zone", code == 200
      and zone_ledger and all(row["location_id"] == 2 for row in zone_ledger), zone_ledger)
_, zone_summary = call("GET", "/api/reports/stock-summary", token=zone_t)
check("zone admin stock summary excludes parent warehouse",
      {row["location_id"] for row in zone_summary} == {2}, zone_summary)
code, _ = call("POST", "/api/stock/counts", token=zone_t, body={"location_id": 1})
check("zone admin cannot start a stock count at the warehouse", code == 403)

code, _ = call("POST", "/api/stock/transfers", token=super_t, body={
    "from_location_id": 2, "to_location_id": 3, "items": [{"book_id": 1, "qty": 1}],
})
check("stock cannot be issued from a zone", code == 400)
code, _ = call("POST", "/api/stock/transfers", token=super_t, body={
    "from_location_id": 1, "to_location_id": 3, "items": [{"book_id": 5, "qty": 1}],
})
check("issue is blocked when warehouse stock is empty", code == 400)
code, _ = call("POST", "/api/stock/transfers", token=super_t, body={
    "from_location_id": 1, "to_location_id": 3,
    "items": [{"book_id": 1, "qty": 3}, {"book_id": 1, "qty": 3}],
})
check("duplicate transfer lines cannot exceed warehouse stock", code == 400)

# --- POS billing at temple ---
code, inv = call("POST", "/api/pos/invoices", token=temple_t, body={
    "location_id": None, "customer_name": "Walk-in", "customer_phone": "9999999999",
    "payment_ref": None, "payment_mode": "cash",
    "items": [{"barcode": "9780892132517", "qty": 2}],
})
check("zone-mapped temple POS invoice (2 x 450)", code == 200 and inv["total"] == 900.0, inv)
check("invoice number uses the billing user's name", str(inv["invoice_number"]).startswith("INV-templeAdmin-"))
code, no_phone_invoice = call("POST", "/api/pos/invoices", token=temple_t, body={
    "customer_name": "Walk-in", "items": [{"barcode": "9780892131343", "qty": 1}],
})
check("invoice requires a phone number", code == 400 and no_phone_invoice.get("detail") == "Phone number is required")

_, ts = call("GET", "/api/stock?location_id=2", token=super_t)
check("stock decremented on sale", qty(ts, 2, 2) == 8)

code, _ = call("POST", "/api/pos/invoices", token=temple_t, body={
    "customer_name": "Walk-in", "customer_phone": "9999999999",
    "items": [{"barcode": "9780892131343", "qty": 9999}],
})
check("oversell rejected", code == 400)
code, _ = call("POST", "/api/pos/invoices", token=temple_t, body={
    "customer_name": "Walk-in", "customer_phone": "9999999999",
    "items": [{"barcode": "9780892131343", "qty": 3}, {"barcode": "9780892131343", "qty": 3}],
})
check("duplicate invoice lines cannot oversell available stock", code == 400)

code, _ = call("POST", "/api/pos/invoices", token=dist_t, body={
    "location_id": 1, "customer_name": "Walk-in", "customer_phone": "9999999999", "payment_mode": "cash",
    "items": [{"barcode": "9780892131565", "qty": 1}],
})
check("distributor cannot bill from warehouse", code == 403)

code, led = call("POST", "/api/pos/invoices", token=dist_t, body={
    "customer_name": "Temple Zone", "customer_phone": "9999999999", "payment_mode": "cash",
    "items": [{"barcode": "9780892131565", "qty": 4}],
})
check("distributor billing at zone", code == 200 and led["total"] == 400.0, led)
code, dist_invoices = call("GET", "/api/pos/invoices", token=dist_t)
_, dist_me = call("GET", "/api/auth/me", token=dist_t)
check("distributor invoice history includes only own bills",
      code == 200 and any(row["id"] == led["invoice_id"] for row in dist_invoices)
      and all(row["cashier_id"] == dist_me["user"]["id"] for row in dist_invoices))
code, _ = call("GET", f"/api/pos/invoices/{inv['invoice_id']}", token=dist_t)
check("distributor cannot open another user's bill", code == 403)
code, own_invoice = call("GET", f"/api/pos/invoices/{led['invoice_id']}", token=dist_t)
check("distributor can open own bill", code == 200 and own_invoice["cashier_id"] == dist_me["user"]["id"])
code, _ = call("DELETE", f"/api/pos/invoices/{led['invoice_id']}", token=dist_t)
check("distributor cannot delete bills", code == 403)
_, distributor_stock = call("GET", "/api/stock", token=dist_t)
check("distributor sees assigned-zone stock only",
      {row["location_id"] for row in distributor_stock} == {2})
_, parent_stock = call("GET", "/api/stock?location_id=1", token=dist_t)
check("distributor cannot view parent warehouse stock", not parent_stock)
code, _ = call("GET", "/api/stock/ledger", token=dist_t)
check("distributor cannot view stock ledger", code == 403)
code, _ = call("GET", "/api/stock/transfers", token=dist_t)
check("distributor cannot view transfers", code == 403)
code, _ = call("GET", "/api/users", token=dist_t)
check("distributor cannot manage users", code == 403)
code, _ = call("GET", "/api/reports/sales", token=dist_t)
check("distributor cannot access sales reports", code == 403)

code, zone_sales = call("GET", "/api/reports/distributor-sales", token=zone_t)
check("zone leader sees distributor book and payment sales", code == 200 and any(
    row["invoice_number"] == led["invoice_number"] and row["distributor_name"] == "Distributor"
    and row["book_title"] == "Raja-vidya" and row["payment_mode"] == "cash" and row["qty"] == 4
    for row in zone_sales
), zone_sales)
code, admin_sales = call("GET", "/api/reports/distributor-sales", token=super_t)
check("super admin sees distributor sales", code == 200 and any(row["invoice_number"] == led["invoice_number"] for row in admin_sales))
other_zone_t = login("leader-niradhar-ashram", "zone123")
code, other_zone_sales = call("GET", "/api/reports/distributor-sales", token=other_zone_t)
check("zone report excludes another zone's distributor sales", code == 200 and all(
    row["invoice_number"] != led["invoice_number"] for row in other_zone_sales
))
code, _ = call("GET", "/api/reports/distributor-sales", token=dist_t)
check("distributor cannot access distributor sales report", code == 403)

_, inv_detail = call("GET", f"/api/pos/invoices/{inv['invoice_id']}", token=temple_t)
check("invoice detail has items", len(inv_detail["items"]) == 1)
check("invoice detail shows cashier display name", inv_detail["cashier_name"] == "Temple Admin")

req = urllib.request.Request(f"{BASE}/api/pos/invoices/{inv['invoice_id']}/print")
req.add_header("Authorization", "Bearer " + temple_t)
with urllib.request.urlopen(req) as resp:
    html_body = resp.read().decode()
check("printable invoice HTML", "<html" in html_body.lower() or "doctype" in html_body.lower())
check("printed invoice shows distributor display name", "Temple Admin" in html_body)

req = urllib.request.Request(f"{BASE}/api/pos/invoices/{inv['invoice_id']}/pdf")
req.add_header("Authorization", "Bearer " + temple_t)
with urllib.request.urlopen(req) as resp:
    pdf_body = resp.read()
    pdf_disposition = resp.headers.get("Content-Disposition", "")
check("invoice PDF download", pdf_body.startswith(b"%PDF-")
      and "attachment" in pdf_disposition and ".pdf" in pdf_disposition)

# --- public invoice share links ---
check("invoice detail exposes share token", bool(inv_detail.get("share_token")), inv_detail)

with urllib.request.urlopen(f"{BASE}/i/{inv_detail['share_token']}") as resp:
    public_html = resp.read().decode()
check("public invoice page opens without login",
      "Temple Admin" in public_html and inv_detail["invoice_number"] in public_html)
check("public invoice page offers a PDF download",
      f"/i/{inv_detail['share_token']}/pdf" in public_html)

with urllib.request.urlopen(f"{BASE}/i/{inv_detail['share_token']}/pdf") as resp:
    public_pdf = resp.read()
    public_disposition = resp.headers.get("Content-Disposition", "")
check("public invoice PDF downloads without login",
      public_pdf.startswith(b"%PDF-") and "attachment" in public_disposition)

try:
    urllib.request.urlopen(f"{BASE}/i/not-a-real-token")
    invalid_token_status = 200
except urllib.error.HTTPError as error:
    invalid_token_status = error.code
check("unknown share token returns 404", invalid_token_status == 404)

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
    "location_id": 1, "reason": None, "items": [{"book_id": 3, "qty": 2}],
})
check("return recorded", code == 200, ret)
rid = ret["id"]
code, _ = call("PATCH", f"/api/stock/returns/{rid}/process?credit_location_id=2", token=zone_t)
check("zone leader processes return (credits stock)", code == 200)

# --- damaged write-off ---
code, dmg = call("POST", "/api/stock/damaged", token=temple_t, body={
    "location_id": 1, "book_id": 4, "qty": 1, "reason_code": "STAINED", "note": None,
})
check("damaged write-off", code == 200, dmg)
code, _ = call("POST", "/api/stock/damaged", token=temple_t, body={
    "location_id": 1, "book_id": 4, "qty": 1, "reason_code": "NOPE",
})
check("invalid reason code rejected", code == 400)

# --- RBAC / scope ---
code, _ = call("GET", "/api/users", token=temple_t)
check("temple user cannot list users", code == 403)
code, _ = call("POST", "/api/books", token=temple_t, body={"title": "x", "barcode": "1"})
check("temple user cannot create books", code == 403)
code, _ = call("GET", "/api/users", token=zone_t)
check("zone leader lists scoped users", code == 200)
code, _ = call("GET", "/api/audit", token=temple_t)
check("temple user cannot view audit trail", code == 403)
code, _ = call("GET", "/api/audit", token=zone_t)
check("zone leader can view audit trail", code == 200)

# --- reports ---
_, sales = call("GET", "/api/reports/sales", token=super_t)
check("sales report has rows", len(sales) >= 1)
_, byb = call("GET", "/api/reports/sales-by-book", token=super_t)
check("best sellers report", len(byb) >= 1 and byb[0]["revenue"] > 0)
code, daily = call("GET", "/api/reports/daily-zone-sales", token=super_t)
check("daily zone sales covers every zone",
      code == 200 and len(daily["zones"]) == 6 and daily["total"] > 0, daily)
check("daily zone totals add up",
      daily["total"] == sum(daily["totals"].values())
      and all(row["total"] == sum(row["zones"].values()) for row in daily["rows"]), daily)
code, zone_daily = call("GET", "/api/reports/daily-zone-sales", token=zone_t)
check("zone admin sees only own daily zone sales",
      code == 200 and [zone["id"] for zone in zone_daily["zones"]] == [2], zone_daily)
code, _ = call("GET", "/api/reports/daily-zone-sales", token=temple_t)
check("temple user cannot view daily zone sales", code == 403)
_, ssum = call("GET", "/api/reports/stock-summary", token=super_t)
check("stock summary lists all 7 locations", len(ssum) == 7)
_, trail = call("GET", "/api/audit", token=super_t)
check("audit trail logged actions", len(trail) >= 10)
code, audit_opts = call("GET", "/api/audit/options", token=super_t)
check("audit filter options available", code == 200 and len(audit_opts["actions"]) > 0 and len(audit_opts["users"]) > 0)
code, filtered_audit = call("GET", "/api/audit?q=LOGIN&limit=500", token=super_t)
check("audit search filter", code == 200 and len(filtered_audit) > 0 and all(
    "LOGIN" in " ".join(str(row.get(k) or "") for k in ("username", "action", "entity", "details")).upper()
    for row in filtered_audit
))
code, login_audit = call("GET", "/api/audit?action=LOGIN", token=super_t)
check("audit action filter", code == 200 and len(login_audit) > 0 and all(row["action"] == "LOGIN" for row in login_audit))

# --- zone overview and distributor drill-down ---
code, zones = call("GET", "/api/reports/zones", token=super_t)
check("zone overview lists every zone", code == 200 and len(zones) == 6, zones)
zone2 = next((z for z in zones if z["id"] == 2), {})
check("zone overview includes stock and cash/UPI totals", zone2.get("stock_units", 0) > 0
      and zone2.get("stock_value", 0) > 0 and zone2.get("distributor_count") == 1
      and zone2.get("invoice_count") == 2 and zone2.get("revenue") == 1300.0
      and zone2.get("cash_total") == 1300.0 and zone2.get("upi_total") == 0, zone2)
code, my_zones = call("GET", "/api/reports/zones", token=zone_t)
check("zone admin sees only own zone", code == 200 and len(my_zones) == 1 and my_zones[0]["id"] == 2)
code, _ = call("GET", "/api/reports/zones", token=temple_t)
check("temple user cannot view zone overview", code == 403)

code, zone_distributors = call("GET", "/api/reports/distributors?zone_id=2", token=zone_t)
check("zone admin sees distributor totals", code == 200 and any(
    d["username"] == "distributor" and d["invoice_count"] == 1 and d["revenue"] == 400.0
    and d["upi_total"] == 0 and d["cash_total"] == 400.0 and d["books_sold"] == 4
    for d in zone_distributors
), zone_distributors)
code, _ = call("GET", "/api/reports/distributors?zone_id=3", token=zone_t)
check("zone admin cannot list another zone's distributors", code == 403)
code, dist_detail = call("GET", f"/api/reports/distributors/{dist_me['user']['id']}", token=zone_t)
check("distributor detail includes invoices and books", code == 200
      and dist_detail["distributor"]["username"] == "distributor"
      and len(dist_detail["invoices"]) == 1 and len(dist_detail["books"]) == 1
      and dist_detail["books"][0]["qty"] == 4 and dist_detail["summary"]["revenue"] == 400.0, dist_detail)
code, _ = call("GET", f"/api/reports/distributors/{dist_me['user']['id']}", token=other_zone_t)
check("other zone admin cannot view distributor detail", code == 403)

# --- invoice visibility by role ---
code, zone_inv = call("POST", "/api/pos/invoices", token=zone_t, body={
    "customer_name": "Zone Walk-in", "customer_phone": "9999999999",
    "payment_mode": "cash", "items": [{"barcode": "9780892131343", "qty": 1}],
})
check("zone admin can bill at own zone", code == 200
      and zone_inv["invoice_number"] == "INV-leader-temple-zone-0001", zone_inv)

code, zone_inv2 = call("POST", "/api/pos/invoices", token=zone_t, body={
    "customer_name": "Zone Walk-in 2", "customer_phone": "9999999999",
    "payment_mode": "cash", "items": [{"barcode": "9780892131343", "qty": 1}],
})
check("invoice numbers increment per user", code == 200
      and zone_inv2["invoice_number"] == "INV-leader-temple-zone-0002", zone_inv2)
code, _ = call("DELETE", f"/api/pos/invoices/{zone_inv2['invoice_id']}", token=super_t)
try:
    urllib.request.urlopen(f"{BASE}/i/{zone_inv2['share_token']}")
    deleted_token_status = 200
except urllib.error.HTTPError as error:
    deleted_token_status = error.code
check("deleted invoice share link returns 404", deleted_token_status == 404)
check("super admin can delete a zone admin bill", code == 200)
code, zone_inv3 = call("POST", "/api/pos/invoices", token=zone_t, body={
    "customer_name": "Zone Walk-in 3", "customer_phone": "9999999999",
    "payment_mode": "cash", "items": [{"barcode": "9780892131343", "qty": 1}],
})
check("deleted invoice numbers are not reused", code == 200
      and zone_inv3["invoice_number"] == "INV-leader-temple-zone-0003", zone_inv3)

_, super_invoices = call("GET", "/api/pos/invoices", token=super_t)
super_invoice_ids = {row["id"] for row in super_invoices}
check("super admin sees invoices billed by zone admins",
      admin_invoice["invoice_id"] in super_invoice_ids and zone_inv["invoice_id"] in super_invoice_ids)
zone_bill = next((row for row in super_invoices if row["id"] == zone_inv["invoice_id"]), {})
check("super admin sees who billed each invoice",
      zone_bill.get("cashier_name") == "Zone Leader - Temple Zone", zone_bill)

code, zone_inv_list = call("GET", "/api/pos/invoices", token=zone_t)
zone_inv_ids = {row["id"] for row in zone_inv_list}
check("zone admin sees only own-zone invoices", code == 200
      and zone_inv["invoice_id"] in zone_inv_ids
      and admin_invoice["invoice_id"] not in zone_inv_ids
      and all(row["location_id"] == 2 for row in zone_inv_list), zone_inv_list)
code, _ = call("GET", f"/api/pos/invoices/{admin_invoice['invoice_id']}", token=zone_t)
check("zone admin cannot open a warehouse invoice", code == 403)
code, _ = call("GET", f"/api/pos/invoices/{admin_invoice['invoice_id']}/print", token=zone_t)
check("zone admin cannot print a warehouse invoice", code == 403)
code, _ = call("DELETE", f"/api/pos/invoices/{admin_invoice['invoice_id']}", token=zone_t)
check("zone admin cannot delete a warehouse invoice", code == 403)
code, super_zone_bill = call("GET", f"/api/pos/invoices/{zone_inv['invoice_id']}", token=super_t)
check("super admin can open a zone admin's bill", code == 200
      and super_zone_bill["cashier_name"] == "Zone Leader - Temple Zone", super_zone_bill)

code, zone_sales = call("GET", "/api/reports/sales", token=zone_t)
check("zone admin sales report covers only own zone", code == 200
      and zone_sales and all(row["location_id"] == 2 for row in zone_sales), zone_sales)
code, all_sales = call("GET", "/api/reports/sales", token=super_t)
check("super admin sales report covers every location", code == 200
      and {row["location_id"] for row in all_sales} >= {1, 2}, all_sales)

_, zone_bills = call("GET", "/api/reports/zones/2/invoices", token=zone_t)
check("zone invoice list includes zone admin bills", code == 200
      and any(row["id"] == zone_inv["invoice_id"] for row in zone_bills), zone_bills)
_, super_zone_bills = call("GET", "/api/reports/zones/2/invoices", token=super_t)
check("super admin can list any zone's invoices",
      any(row["id"] == zone_inv["invoice_id"] for row in super_zone_bills), super_zone_bills)
code, _ = call("GET", "/api/reports/zones/3/invoices", token=zone_t)
check("zone admin cannot list another zone's invoices", code == 403)

# --- payment in (cash handover to the temple) ---
_, zone_me = call("GET", "/api/auth/me", token=zone_t)
zone_user_id = zone_me["user"]["id"]

code, pay_admins = call("GET", "/api/payments/zone-admins", token=temple_t)
check("temple admin can list zone admins for payment in", code == 200
      and any(u["username"] == "leader-temple-zone" for u in pay_admins), pay_admins)
code, zone_pay_admins = call("GET", "/api/payments/zone-admins", token=zone_t)
check("zone leader sees only themselves for payment in", code == 200
      and len(zone_pay_admins) == 1 and zone_pay_admins[0]["id"] == zone_user_id, zone_pay_admins)

code, dist_cash = call("POST", "/api/pos/invoices", token=dist_t, body={
    "customer_name": "Zone collection", "customer_phone": "9999999999",
    "payment_mode": "cash",
    "items": [{"barcode": "9780892131343", "qty": 1}],
})
check("distributor cash bill in the zone", code == 200 and dist_cash["total"] == 300.0, dist_cash)

# A zone leader can take several bills from the same distributor in one action.
bulk_bills = []
for customer in ("Bulk collection one", "Bulk collection two"):
    code, bill = call("POST", "/api/pos/invoices", token=dist_t, body={
        "customer_name": customer, "customer_phone": "9999999999",
        "payment_mode": "cash",
        "items": [{"barcode": "9780892132517", "qty": 1}],
    })
    bulk_bills.append(bill)
code, bulk_result = call("POST", "/api/pos/invoices/collect", token=zone_t, body={
    "invoice_ids": [bulk_bills[1]["invoice_id"], bulk_bills[0]["invoice_id"]],
    "amount": 99999, "method": "cash",
})
check("combined collection rejects an overpayment", code == 400)
code, bulk_result = call("POST", "/api/pos/invoices/collect", token=zone_t, body={
    "invoice_ids": [bulk_bills[1]["invoice_id"], bulk_bills[0]["invoice_id"]],
    "method": "cash",
})
check("zone leader collects several distributor bills at once", code == 200
      and bulk_result["invoice_count"] == 2
      and bulk_result["total_collected"] == sum(b["total"] for b in bulk_bills)
      and bulk_result["balance_due"] == 0, bulk_result)
for bill in bulk_bills:
    call("DELETE", f"/api/pos/invoices/{bill['invoice_id']}", token=super_t)
code, _ = call("GET", f"/api/pos/invoices/{bulk_bills[0]['invoice_id']}", token=super_t)
check("bulk collected bills can be removed", code == 404)

# A distributor bill is due to the zone at billing; its cash becomes remittable
# only after the zone collects from the distributor.
for billed in (led, dist_cash):
    code, collected = call("POST", f"/api/pos/invoices/{billed['invoice_id']}/payments", token=zone_t, body={
        "amount": billed["total"], "method": "cash", "note": "Collected from distributor",
    })
    check(f"zone leader collects a distributor bill of {billed['total']}", code == 200
          and collected["status"] == "paid" and collected["balance_due"] == 0, collected)
code, _ = call("POST", f"/api/pos/invoices/{dist_cash['invoice_id']}/payments", token=dist_t, body={
    "amount": 1, "method": "cash",
})
check("distributors cannot collect from their own bill", code == 403)

code, pending = call("GET", f"/api/payments/pending?user_id={zone_user_id}", token=temple_t)
pending_numbers = {p["invoice_number"] for p in pending}
check("pending cash receipts cover the whole zone team", code == 200
      and pending_numbers == {zone_inv["invoice_number"], zone_inv3["invoice_number"],
                              dist_cash["invoice_number"], led["invoice_number"]}, pending)
check("pending receipts show who billed them", any(
    p["invoice_number"] == dist_cash["invoice_number"] and p["cashier_name"] == "Distributor"
    for p in pending
), pending)
check("pending receipts expose cashier identity for the distributor filter", any(
    p["invoice_number"] == dist_cash["invoice_number"]
    and p.get("cashier_id") == dist_me["user"]["id"]
    and p.get("cashier_role") == "distributor"
    for p in pending
), pending)

code, summary = call("GET", "/api/payments/pending-summary", token=temple_t)
zone_summary = next((z for z in summary if z["zone_id"] == 2), {})
check("temple sees how much it has to collect from each zone", code == 200
      and zone_summary.get("amount") == 1300.0 and zone_summary.get("bills") == 4,
      zone_summary)
check("temple summary tallies cash collected and handed over", code == 200
      and zone_summary.get("cash_collected") == 1300.0
      and zone_summary.get("cash_remitted") == 0.0, zone_summary)
check("temple summary starts with no online money in the zone", code == 200
      and zone_summary.get("online_received") == 0
      and zone_summary.get("total_collected") == 1300.0, zone_summary)
code, super_summary = call("GET", "/api/payments/pending-summary", token=super_t)
check("super admin sees the same temple collection summary", code == 200
      and any(z["zone_id"] == 2 and z["amount"] == 1300.0 for z in super_summary), super_summary)
code, zone_reports = call("GET", "/api/reports/zones", token=super_t)
zone_row = next((z for z in zone_reports if z["id"] == 2), {})
check("dashboard zone report shows the temple's pending cash", code == 200
      and zone_row.get("pending_cash") == 1300.0, zone_row)
code, dist_reports = call("GET", "/api/reports/distributors?zone_id=2", token=super_t)
dist_row = next((d for d in dist_reports if d["username"] == "distributor"), {})
dist_pending = sum(p["amount"] for p in pending if p.get("cashier_id") == dist_me["user"]["id"])
check("dashboard distributor report shows cash still to hand to the temple", code == 200
      and dist_row.get("pending_to_temple") == dist_pending, dist_row)
check("temple summary names the zone leader", any(
    z["zone_id"] == 2 and any(a["username"] == "leader-temple-zone" for a in z["zone_admins"])
    for z in summary
), summary)
code, zone_summary_own = call("GET", "/api/payments/pending-summary", token=zone_t)
check("zone leader sees their own zone's tally", code == 200
      and [z["zone_id"] for z in zone_summary_own] == [2]
      and zone_summary_own[0]["amount"] == 1300.0
      and zone_summary_own[0]["cash_collected"] == 1300.0, zone_summary_own)
code, other_zone_summary = call("GET", "/api/payments/pending-summary", token=other_zone_t)
check("another zone leader never sees this zone's tally", code == 200
      and all(z["zone_id"] != 2 for z in other_zone_summary), other_zone_summary)
code, _ = call("GET", "/api/payments/pending-summary", token=dist_t)
check("distributor cannot see the zone tally", code == 403)

zone_team_numbers = [zone_inv["invoice_number"], zone_inv3["invoice_number"],
                     dist_cash["invoice_number"], led["invoice_number"]]
code, preview = call("POST", "/api/payments/preview", token=temple_t, body={
    "user_id": zone_user_id,
    "receipt_numbers": zone_team_numbers,
})
check("payment preview auto-sums the zone team's receipts", code == 200
      and preview["total"] == 1300.0 and preview["count"] == 4 and not preview["errors"], preview)
check("payment preview labels distributor cash", any(
    i["cashier_name"] == "Distributor" for i in preview["items"]
), preview)
code, preview_bad = call("POST", "/api/payments/preview", token=temple_t, body={
    "user_id": zone_user_id,
    "receipt_numbers": [inv["invoice_number"], "NO-SUCH-RECEIPT"],
})
check("payment preview rejects temple-billed and unknown receipts", code == 200
      and preview_bad["count"] == 0 and len(preview_bad["errors"]) == 2, preview_bad)
_, other_zone_me = call("GET", "/api/auth/me", token=other_zone_t)
other_zone_user_id = other_zone_me["user"]["id"]
code, _ = call("GET", f"/api/payments/pending?user_id={other_zone_user_id}", token=zone_t)
check("zone leader cannot view another zone's pending cash", code == 403)
code, _ = call("POST", "/api/payments/preview", token=zone_t, body={
    "user_id": other_zone_user_id, "receipt_numbers": [zone_inv["invoice_number"]],
})
check("zone leader cannot preview another zone's handover", code == 403)
code, _ = call("POST", "/api/payments", token=zone_t, body={
    "user_id": other_zone_user_id, "receipt_numbers": [zone_inv["invoice_number"]],
})
check("zone leader cannot record another zone's handover", code == 403)

code, dist_cash_self = call("POST", "/api/pos/invoices", token=dist_t, body={
    "customer_name": "Zone leader handover", "customer_phone": "9999999999",
    "payment_mode": "cash",
    "items": [{"barcode": "9780892132517", "qty": 1}],
})
check("distributor cash bill for the zone leader's own handover", code == 200
      and dist_cash_self["total"] == 450.0 and dist_cash_self["status"] == "unpaid", dist_cash_self)
code, self_collected = call("POST", f"/api/pos/invoices/{dist_cash_self['invoice_id']}/payments", token=zone_t, body={
    "amount": 450.0, "method": "cash", "note": "Collected from distributor",
})
check("zone leader collects their distributor's bill before handover", code == 200
      and self_collected["status"] == "paid", self_collected)
code, self_preview = call("POST", "/api/payments/preview", token=zone_t, body={
    "user_id": zone_user_id, "receipt_numbers": [dist_cash_self["invoice_number"]],
})
check("zone leader previews their distributor's cash", code == 200
      and self_preview["total"] == 450.0 and not self_preview["errors"], self_preview)
code, zone_payment = call("POST", "/api/payments", token=zone_t, body={
    "user_id": zone_user_id, "receipt_numbers": [dist_cash_self["invoice_number"]],
    "note": "Handed over by the zone leader",
})
check("zone leader records their distributor's cash handover", code == 200
      and zone_payment["amount"] == 450.0
      and zone_payment["submitted_by"] == zone_user_id
      and zone_payment["recorded_by"] == zone_user_id, zone_payment)
code, pending_self = call("GET", f"/api/payments/pending?user_id={zone_user_id}", token=zone_t)
check("self recorded handover leaves the zone leader's pending list", code == 200
      and all(p["invoice_number"] != dist_cash_self["invoice_number"] for p in pending_self),
      pending_self)

code, payment = call("POST", "/api/payments", token=temple_t, body={
    "user_id": zone_user_id,
    "receipt_numbers": zone_team_numbers,
    "note": "Counted at the temple",
})
check("temple admin records a cash handover", code == 200
      and payment["receipt_number"] == "RCV-templeAdmin-0001"
      and payment["amount"] == 1300.0 and len(payment["items"]) == 4, payment)
check("payment in includes distributor cash", any(
    i["cashier_name"] == "Distributor" for i in payment["items"]
), payment)
check("payment in names the zone admin and the confirmer",
      payment.get("zone_admin_name") == "Zone Leader - Temple Zone"
      and payment.get("recorded_by_name") == "Temple Admin", payment)

code, _ = call("POST", "/api/payments", token=temple_t, body={
    "user_id": zone_user_id, "receipt_numbers": [zone_inv["invoice_number"]],
})
check("a receipt cannot be handed over twice", code == 400)
code, pending_after = call("GET", f"/api/payments/pending?user_id={zone_user_id}", token=temple_t)
check("handed-over receipts leave the pending list", code == 200 and not pending_after, pending_after)
code, summary_after = call("GET", "/api/payments/pending-summary", token=temple_t)
zone_after = next((z for z in summary_after if z["zone_id"] == 2), {})
check("temple tally shows no cash left to collect after handover", code == 200
      and zone_after.get("amount") == 0.0
      and zone_after.get("cash_collected") == 1750.0
      and zone_after.get("cash_remitted") == 1750.0, zone_after)

code, zone_payments = call("GET", "/api/payments", token=zone_t)
check("zone admin sees the temple confirmation", code == 200
      and any(p["id"] == payment["id"] for p in zone_payments), zone_payments)
code, zone_payment = call("GET", f"/api/payments/{payment['id']}", token=zone_t)
check("zone admin can open the cash receipt", code == 200
      and len(zone_payment["items"]) == 4, zone_payment)
code, other_zone_payments = call("GET", "/api/payments", token=other_zone_t)
check("another zone admin does not see the handover", code == 200 and not other_zone_payments)
code, _ = call("GET", f"/api/payments/{payment['id']}", token=other_zone_t)
check("another zone admin cannot open the cash receipt", code == 403)
code, super_payments = call("GET", "/api/payments", token=super_t)
check("super admin sees every cash handover", code == 200
      and any(p["id"] == payment["id"] for p in super_payments), super_payments)
code, _ = call("DELETE", f"/api/pos/invoices/{zone_inv['invoice_id']}", token=super_t)
check("a handed-over invoice cannot be deleted", code == 409)

req = urllib.request.Request(f"{BASE}/api/payments/{payment['id']}/print")
req.add_header("Authorization", "Bearer " + zone_t)
with urllib.request.urlopen(req) as resp:
    receipt_body = resp.read().decode()
check("printable cash receipt HTML", "cash payment-in receipt" in receipt_body.lower())
check("printed receipt shows the zone admin", "Zone Leader - Temple Zone" in receipt_body)
check("printed cash receipt labels its date as IST", " IST" in receipt_body)

# --- super admin invoice editing ---
_, zone_stock_before_edit = call("GET", "/api/stock?location_id=2&book_id=1", token=super_t)
zone_qty_before_edit = qty(zone_stock_before_edit, 1, 2)
code, edit_inv = call("POST", "/api/pos/invoices", token=zone_t, body={
    "customer_name": "Editable Customer", "customer_phone": "9999999999",
    "payment_mode": "cash", "items": [{"barcode": "9780892131343", "qty": 1}],
})
check("invoice created for editing", code == 200, edit_inv)
code, _ = call("PATCH", f"/api/pos/invoices/{edit_inv['invoice_id']}", token=zone_t, body={
    "customer_name": "Zone edit attempt",
})
check("zone admin cannot edit invoices", code == 403)
code, edited_invoice = call("PATCH", f"/api/pos/invoices/{edit_inv['invoice_id']}", token=super_t, body={
    "customer_name": "Corrected Customer", "customer_phone": "9123456780",
    "payment_ref": "CASH-EDIT", "discount": 25,
})
check("super admin can edit invoice details", code == 200
      and edited_invoice["customer_name"] == "Corrected Customer"
      and edited_invoice["customer_phone"] == "9123456780"
      and edited_invoice["payment_ref"] == "CASH-EDIT"
      and edited_invoice["discount"] == 25, edited_invoice)
check("edited invoice total is recalculated", edited_invoice["total"] == round(edited_invoice["subtotal"] - 25, 2), edited_invoice)
edit_unit_price = float(edited_invoice["items"][0]["unit_price"])
code, edited_items = call("PATCH", f"/api/pos/invoices/{edit_inv['invoice_id']}", token=super_t, body={
    "items": [{"barcode": "9780892131343", "qty": 2}],
})
check("super admin can edit invoice items", code == 200
      and edited_items["items"][0]["qty"] == 2
      and edited_items["subtotal"] == round(edit_unit_price * 2, 2), edited_items)
_, zone_stock_after_edit = call("GET", "/api/stock?location_id=2&book_id=1", token=super_t)
check("increasing an invoice item moves zone stock",
      qty(zone_stock_after_edit, 1, 2) == zone_qty_before_edit - 2, zone_stock_after_edit)
code, edited_back = call("PATCH", f"/api/pos/invoices/{edit_inv['invoice_id']}", token=super_t, body={
    "items": [{"barcode": "9780892131343", "qty": 1}],
})
check("decreasing an invoice item restocks", code == 200 and edited_back["items"][0]["qty"] == 1, edited_back)
_, zone_stock_restored = call("GET", "/api/stock?location_id=2&book_id=1", token=super_t)
check("stock restored after invoice item decrease",
      qty(zone_stock_restored, 1, 2) == zone_qty_before_edit - 1, zone_stock_restored)
code, _ = call("PATCH", f"/api/pos/invoices/{edit_inv['invoice_id']}", token=super_t, body={
    "items": [{"barcode": "NO-SUCH-BARCODE", "qty": 1}],
})
check("editing with an unknown barcode is rejected", code == 400)
code, _ = call("PATCH", f"/api/pos/invoices/{edit_inv['invoice_id']}", token=super_t, body={"items": []})
check("an invoice must keep at least one item", code == 400)
code, _ = call("PATCH", f"/api/pos/invoices/{zone_inv['invoice_id']}", token=super_t, body={
    "items": [{"barcode": "9780892131343", "qty": 1}],
})
check("handed-over invoice items cannot be edited", code == 409)
code, handed_contact = call("PATCH", f"/api/pos/invoices/{zone_inv['invoice_id']}", token=super_t, body={
    "customer_phone": "9000000001",
})
check("handed-over invoice contact can still be corrected", code == 200
      and handed_contact["customer_phone"] == "9000000001", handed_contact)

# --- distributor self-edit and zone-admin distributor attribution ---
code, self_edit = call("PATCH", f"/api/pos/invoices/{led['invoice_id']}", token=dist_t, body={
    "customer_phone": "9111111111",
})
check("distributor can edit their own bill", code == 200
      and self_edit["customer_phone"] == "9111111111", self_edit)
code, attributed = call("POST", "/api/pos/invoices", token=zone_t, body={
    "customer_name": "Distributor Sale", "customer_phone": "9999999999",
    "payment_mode": "cash", "distributor_id": dist_me["user"]["id"],
    "items": [{"barcode": "9780892132517", "qty": 1}],
})
check("zone admin can bill for a distributor in their zone", code == 200
      and attributed.get("invoice_number", "").startswith("INV-leader-temple-zone-"), attributed)
code, attributed_detail = call("GET", f"/api/pos/invoices/{attributed['invoice_id']}", token=dist_t)
check("distributor can open an invoice credited to them", code == 200
      and attributed_detail["distributor_id"] == dist_me["user"]["id"]
      and attributed_detail["cashier_id"] != dist_me["user"]["id"], attributed_detail)
code, dist_rows = call("GET", "/api/pos/invoices", token=dist_t)
check("distributor history includes invoices credited to them", code == 200
      and any(row["id"] == attributed["invoice_id"] for row in dist_rows))
code, attributed_edit = call("PATCH", f"/api/pos/invoices/{attributed['invoice_id']}", token=dist_t, body={
    "customer_name": "Distributor Corrected",
})
check("distributor can edit an invoice credited to them", code == 200
      and attributed_edit["customer_name"] == "Distributor Corrected", attributed_edit)

code, other_dist = call("POST", "/api/users", token=super_t, body={
    "username": "smoke-distributor-other", "password": "dist123", "name": "Other Distributor",
    "role": "distributor", "zone_id": 3,
})
other_dist_t = login("smoke-distributor-other", "dist123")
code, _ = call("PATCH", f"/api/pos/invoices/{attributed['invoice_id']}", token=other_dist_t, body={
    "customer_name": "Not mine",
})
check("another distributor cannot edit the invoice", code == 403)
code, _ = call("GET", f"/api/pos/invoices/{attributed['invoice_id']}", token=other_dist_t)
check("another distributor cannot open the invoice", code == 403)
code, _ = call("POST", "/api/pos/invoices", token=zone_t, body={
    "customer_name": "Wrong Zone", "customer_phone": "9999999999", "payment_mode": "cash",
    "distributor_id": other_dist.get("id"),
    "items": [{"barcode": "9780892131343", "qty": 1}],
})
check("zone admin cannot bill for another zone's distributor", code == 403)
code, _ = call("DELETE", f"/api/users/{other_dist.get('id')}", token=super_t)
check("removed the other distributor", code == 200)

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
code, _ = call("PATCH", f"/api/locations/{test_location_id}", token=zone_t, body={"name": "Nope"})
check("only super admin can edit locations", code == 403)
code, _ = call("PATCH", f"/api/locations/{test_location_id}", token=super_t, body={
    "code": "SMOKE-LOC-2", "name": "Smoke Test Location Updated", "type": "zone", "parent_id": None,
})
check("location edit updates all fields", code == 200)
code, edited_location = call("GET", f"/api/locations/{test_location_id}", token=super_t)
check("edited location fields persisted", code == 200 and edited_location["code"] == "SMOKE-LOC-2"
      and edited_location["name"] == "Smoke Test Location Updated" and edited_location["type"] == "zone"
      and edited_location["parent_id"] is None, edited_location)
code, _ = call("PATCH", f"/api/locations/{test_location_id}", token=super_t, body={"parent_id": test_location_id})
check("location cannot be its own parent", code == 400)
code, _ = call("PATCH", f"/api/locations/{test_location_id}", token=super_t, body={"code": "TEMPLE"})
check("location code uniqueness on edit", code == 409)
code, _ = call("DELETE", f"/api/locations/{test_location_id}", token=super_t)
check("unused child location can be deleted", code == 200)
code, root_location = call("POST", "/api/locations", token=super_t, body={
    "code": "SMOKE-ROOT", "name": "Smoke Root Location", "type": "warehouse", "parent_id": None,
})
check("root location accepts null parent id", code == 200 and root_location["code"] == "SMOKE-ROOT", root_location)
code, _ = call("DELETE", f"/api/locations/{root_location.get('id')}", token=super_t)
check("unused root location can be deleted", code == 200)

# --- stock adjustment, ledger, and open-count deletion ---
code, adjusted = call("POST", "/api/stock/adjust", token=temple_t, body={
    "book_id": 1, "location_id": 1, "qty": 2, "note": None,
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
code, _ = call("POST", "/api/stock/adjust", token=temple_t, body={
    "book_id": 1, "location_id": 2, "qty": 1,
})
check("temple warehouse staff cannot adjust zone stock", code == 403)
code, _ = call("POST", "/api/stock/adjust", token=zone_t, body={
    "book_id": 1, "location_id": 2, "qty": 1,
})
check("zone leader cannot directly add zone stock", code == 403)
code, _ = call("POST", "/api/stock/adjust", token=dist_t, body={
    "book_id": 1, "location_id": 1, "qty": 1,
})
check("distributor cannot directly adjust warehouse stock", code == 403)
code, _ = call("POST", "/api/stock/adjust", token=super_t, body={
    "book_id": 1, "location_id": 2, "qty": 1,
})
check("admin can only manually adjust warehouse stock", code == 403)
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
transfer_row = next((t for t in transfer_rows if t["id"] == tid), {})
check("transfer list reports line and unit counts",
      transfer_row.get("item_count") == 4 and transfer_row.get("units") == 29, transfer_row)
code, transfer_detail = call("GET", f"/api/stock/transfers/{tid}", token=zone_t)
check("transfer detail includes items", code == 200 and len(transfer_detail["items"]) == 4)
check("transfer detail names the people at each step",
      transfer_detail.get("requested_by_name") == "leader-temple-zone"
      and transfer_detail.get("approved_by_name") == "leader-temple-zone"
      and transfer_detail.get("dispatched_by_name") == "leader-temple-zone"
      and transfer_detail.get("received_by_name") == "leader-temple-zone",
      transfer_detail)
req = urllib.request.Request(BASE + f"/api/stock/transfers/{tid}/print")
req.add_header("Authorization", "Bearer " + zone_t)
with urllib.request.urlopen(req) as resp:
    transfer_print = resp.read().decode()
    transfer_print_status = resp.status
check("transfer print page renders in the detail layout",
      transfer_print_status == 200
      and "Stock transfer" in transfer_print
      and transfer_detail["request_number"] in transfer_print
      and "Books" in transfer_print
      and all(item["title"] in transfer_print for item in transfer_detail["items"]),
      transfer_print[:400])
code, _ = call("GET", f"/api/stock/transfers/{tid}/print", token=other_zone_t)
check("other zone cannot print the transfer", code == 403)
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
_, before_invoice_rows = call("GET", "/api/stock?location_id=2&book_id=1", token=super_t)
before_invoice_delete = qty(before_invoice_rows, 1, 2)
code, disposable_invoice = call("POST", "/api/pos/invoices", token=temple_t, body={
    "customer_name": "Walk-in", "customer_phone": "9999999999",
    "payment_mode": "cash", "items": [{"barcode": "9780892131343", "qty": 1}],
})
disposable_invoice_id = disposable_invoice.get("invoice_id")
code, deletion = call("DELETE", f"/api/pos/invoices/{disposable_invoice_id}", token=temple_t)
check("invoice deletion restores sold stock", code == 200 and deletion["restocked_lines"] == 1)
code, restored_stock = call("GET", "/api/stock?location_id=2&book_id=1", token=super_t)
check("invoice deletion restores exact quantity", qty(restored_stock, 1, 2) == before_invoice_delete)

# --- return deletion should restore the source stock ---
_, warehouse_rows = call("GET", "/api/stock?location_id=1&book_id=1", token=super_t)
before_return_delete = qty(warehouse_rows, 1, 1)
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
    "role": "distributor", "zone_id": 2,
})
check("user creation", code == 200 and created_user["username"] == "smoke-user")
test_user_id = created_user.get("id")
code, temple_user = call("POST", "/api/users", token=super_t, body={
    "username": "smoke-temple-user", "password": "temple-smoke-password", "name": "Smoke Temple User",
    "role": "temple", "zone_id": 2,
})
check("temple user creation requires and accepts a zone", code == 200 and temple_user["username"] == "smoke-temple-user")
code, _ = call("POST", "/api/users", token=super_t, body={
    "username": "smoke-invalid-zone", "password": "invalid-zone-password", "name": "Invalid Zone User",
    "role": "temple", "zone_id": 1,
})
check("user mapping rejects non-zone locations", code == 400)
code, admin_user = call("POST", "/api/users", token=super_t, body={
    "username": "smoke-admin-user", "password": "admin-smoke-password", "name": "Smoke Admin User",
    "role": "super_admin", "zone_id": None,
})
check("super admin creation accepts null zone id", code == 200 and admin_user["username"] == "smoke-admin-user")
code, users = call("GET", "/api/users", token=super_t)
check("user list excludes password hashes", code == 200 and "password_hash" not in str(users))
code, _ = call("PATCH", f"/api/users/{test_user_id}", token=super_t, body={
    "name": "Updated Smoke User", "password": "updated-smoke-password",
})
check("user update and password change", code == 200)
code, _ = call("PATCH", f"/api/users/{test_user_id}", token=super_t, body={"zone_id": 1})
check("user update rejects non-zone mappings", code == 400)
check("updated user password authenticates", bool(login("smoke-user", "updated-smoke-password")))
code, _ = call("PATCH", f"/api/users/{test_user_id}", token=super_t, body={"is_active": 0})
check("user can be disabled", code == 200 and login("smoke-user", "updated-smoke-password") is None)
code, _ = call("PATCH", f"/api/users/{test_user_id}", token=super_t, body={"is_active": 1})
check("user can be re-enabled", code == 200 and bool(login("smoke-user", "updated-smoke-password")))
code, _ = call("DELETE", f"/api/users/{test_user_id}", token=super_t)
check("user with audit history can be deleted", code == 200)
_, audit_after_user_delete = call("GET", "/api/audit?limit=500", token=super_t)
check("deleting user removes their audit history",
      all(row["user_id"] != test_user_id for row in audit_after_user_delete))
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
_, warehouse_before = call("GET", "/api/stock?location_id=1&book_id=1", token=super_t)
_, zone_before = call("GET", "/api/stock?location_id=2&book_id=1", token=super_t)
code, disposable_transfer = call("POST", "/api/stock/transfers", token=super_t, body={
    "from_location_id": 1, "to_location_id": 2, "items": [{"book_id": 1, "qty": 1}],
})
received_transfer_id = disposable_transfer.get("id")
code, _ = call("POST", f"/api/stock/transfers/{received_transfer_id}/approve", token=super_t)
code, _ = call("POST", f"/api/stock/transfers/{received_transfer_id}/dispatch", token=super_t)
code, _ = call("POST", f"/api/stock/transfers/{received_transfer_id}/receive", token=super_t)
code, _ = call("DELETE", f"/api/stock/transfers/{received_transfer_id}", token=super_t)
check("received transfer can be deleted", code == 200)
_, warehouse_after = call("GET", "/api/stock?location_id=1&book_id=1", token=super_t)
_, zone_after = call("GET", "/api/stock?location_id=2&book_id=1", token=super_t)
check("deleting received transfer reverses stock at both locations",
      qty(warehouse_after, 1, 1) == qty(warehouse_before, 1, 1)
      and qty(zone_after, 1, 2) == qty(zone_before, 1, 2))
_, book_three_zone_before = call("GET", "/api/stock?location_id=2&book_id=3", token=super_t)
consumed_transfer_qty = qty(book_three_zone_before, 3, 2) + 1
code, consumed_transfer = call("POST", "/api/stock/transfers", token=super_t, body={
    "from_location_id": 1, "to_location_id": 2, "items": [{"book_id": 3, "qty": consumed_transfer_qty}],
})
consumed_transfer_id = consumed_transfer.get("id")
call("POST", f"/api/stock/transfers/{consumed_transfer_id}/approve", token=super_t)
call("POST", f"/api/stock/transfers/{consumed_transfer_id}/dispatch", token=super_t)
call("POST", f"/api/stock/transfers/{consumed_transfer_id}/receive", token=super_t)
code, _ = call("POST", "/api/pos/invoices", token=temple_t, body={
    "customer_name": "Transfer reversal test", "customer_phone": "9999999999", "payment_mode": "cash",
    "items": [{"barcode": "9780892139479", "qty": consumed_transfer_qty + qty(book_three_zone_before, 3, 2)}],
})
check("received transfer stock can be consumed before deletion", code == 200)
code, _ = call("DELETE", f"/api/stock/transfers/{consumed_transfer_id}", token=super_t)
check("received transfer deletion is blocked when destination stock is consumed", code == 409)

# --- partial payments: distributor settlement and later balance ---
_, all_books = call("GET", "/api/books", token=super_t)
book_seven = next(b for b in all_books if b["barcode"] == "9780892131565")
godown_total = round(float(book_seven["price"]) * 2, 2)

code, _ = call("POST", "/api/pos/invoices", token=super_t, body={
    "location_id": 1, "customer_name": "Walk-in Partial", "customer_phone": "9999999999",
    "payment_mode": "cash", "amount_received": 10,
    "items": [{"barcode": book_seven["barcode"], "qty": 1}],
})
check("walk-in bills must be paid in full", code == 400)

code, deposit = call("POST", "/api/pos/invoices", token=super_t, body={
    "location_id": 1, "customer_name": "Distributor Deposit", "customer_phone": "9999999999",
    "distributor_id": dist_me["user"]["id"],
    "payment_mode": "cash", "amount_received": 80,
    "items": [{"barcode": book_seven["barcode"], "qty": 2}],
})
check("distributor bill accepts a partial collection at billing", code == 200
      and deposit["total"] == godown_total and deposit["amount_paid"] == 80.0
      and deposit["balance_due"] == godown_total - 80 and deposit["status"] == "partial", deposit)

code, deposit_detail = call("GET", f"/api/pos/invoices/{deposit['invoice_id']}", token=super_t)
check("partial invoice detail shows the payment ledger", code == 200
      and len(deposit_detail["payments"]) == 1
      and deposit_detail["payments"][0]["amount"] == 80.0
      and deposit_detail["amount_paid"] == 80.0
      and deposit_detail["balance_due"] == godown_total - 80, deposit_detail)

try:
    with urllib.request.urlopen(
        f"{BASE}/i/{deposit['share_token']}"
    ) as resp:
        partial_public_status = resp.status
        partial_public_body = resp.read().decode()
except urllib.error.HTTPError as error:
    partial_public_status = error.code
    partial_public_body = ""
check("partial invoice public link opens", partial_public_status == 200
      and "balance due" in partial_public_body.lower())

req = urllib.request.Request(f"{BASE}/api/pos/invoices/{deposit['invoice_id']}/print")
req.add_header("Authorization", "Bearer " + super_t)
with urllib.request.urlopen(req) as resp:
    partial_print_body = resp.read().decode()
check("partial invoice prints with a balance", "Balance due" in partial_print_body)

code, _ = call("PATCH", f"/api/pos/invoices/{deposit['invoice_id']}", token=super_t, body={
    "discount": 10,
})
check("partially paid invoice amounts are locked", code == 409)

code, over_payment = call("POST", f"/api/pos/invoices/{deposit['invoice_id']}/payments", token=super_t, body={
    "amount": godown_total, "method": "cash",
})
check("invoice payment rejects more than the balance", code == 400, over_payment)

code, paid_balance = call("POST", f"/api/pos/invoices/{deposit['invoice_id']}/payments", token=super_t, body={
    "amount": godown_total - 80, "method": "upi", "reference": "UPI-BALANCE",
})
check("godown balance can be collected later", code == 200
      and paid_balance["status"] == "paid" and paid_balance["balance_due"] == 0, paid_balance)

_, sales_after_partials = call("GET", "/api/reports/sales", token=super_t)
warehouse_sales = next((row for row in sales_after_partials if row["location_id"] == 1), {})
check("sales report exposes collected and outstanding", warehouse_sales.get("collected", 0) >= godown_total
      and abs(float(warehouse_sales.get("outstanding", -1))
              - max(0, float(warehouse_sales.get("revenue", 0)) - float(warehouse_sales.get("collected", 0)))) < 0.01,
      warehouse_sales)

# --- distributor bill settled by the zone and a partial cash handover ---
_, warehouse_rows_now = call("GET", "/api/stock?location_id=1", token=super_t)
books_by_id = {b["id"]: b for b in all_books}
credit_book = next(
    (books_by_id[row["book_id"]] for row in warehouse_rows_now
     if row["qty"] >= 5 and row["book_id"] in books_by_id
     and books_by_id[row["book_id"]]["barcode"] != book_seven["barcode"]),
    book_seven,
)
code, topup = call("POST", "/api/stock/transfers", token=super_t, body={
    "from_location_id": 1, "to_location_id": 2,
    "items": [{"book_id": credit_book["id"], "qty": 5}],
})
topup_id = topup.get("id")
call("POST", f"/api/stock/transfers/{topup_id}/approve", token=super_t)
call("POST", f"/api/stock/transfers/{topup_id}/dispatch", token=super_t)
call("POST", f"/api/stock/transfers/{topup_id}/receive", token=super_t)
check("stock moved to the zone for the distributor sale", code == 200, topup)

code, credit = call("POST", "/api/pos/invoices", token=dist_t, body={
    "customer_name": "Distributor Customer", "customer_phone": "9999999999",
    "payment_mode": "ledger",
    "items": [{"barcode": credit_book["barcode"], "qty": 1}],
})
check("distributor bill is due to the zone at billing", code == 200
      and credit["status"] == "unpaid" and credit["amount_paid"] == 0
      and credit["balance_due"] == credit["total"]
      and credit["distributor_id"] == dist_me["user"]["id"], credit)

code, credit_detail = call("GET", f"/api/pos/invoices/{credit['invoice_id']}", token=dist_t)
check("distributor bill starts with no payment rows", code == 200
      and credit_detail["payments"] == [] and credit_detail["balance_due"] == credit["total"], credit_detail)

code, _ = call("POST", f"/api/pos/invoices/{credit['invoice_id']}/payments", token=dist_t, body={
    "amount": 50, "method": "cash", "note": "Collected from customer",
})
check("distributors cannot collect from their own bill", code == 403)

code, credit_payment = call("POST", f"/api/pos/invoices/{credit['invoice_id']}/payments", token=zone_t, body={
    "amount": 50, "method": "cash", "note": "Collected from distributor",
})
check("zone leader collects a partial payment from the distributor", code == 200
      and credit_payment["status"] == "partial"
      and credit_payment["amount_paid"] == 50
      and credit_payment["balance_due"] == credit["total"] - 50, credit_payment)

_, dist_after_credit = call("GET", f"/api/reports/distributors/{dist_me['user']['id']}", token=zone_t)
credit_row = next((row for row in dist_after_credit["invoices"] if row["id"] == credit["invoice_id"]), {})
check("distributor report shows the outstanding balance", credit_row.get("amount_paid") == 50
      and credit_row.get("balance_due") == credit["total"] - 50
      and dist_after_credit["summary"]["outstanding"] >= credit["total"] - 50, credit_row)

code, pending_credit = call("GET", f"/api/payments/pending?user_id={zone_user_id}", token=temple_t)
credit_pending = next((row for row in pending_credit if row["invoice_number"] == credit["invoice_number"]), {})
check("cash collected from the distributor is remittable", code == 200
      and credit_pending.get("remittable") == 50
      and credit_pending.get("cash_collected") == 50
      and credit_pending.get("cash_remitted") == 0, credit_pending)

code, partial_preview = call("POST", "/api/payments/preview", token=temple_t, body={
    "user_id": zone_user_id, "receipt_numbers": [credit["invoice_number"]], "amount": 20,
})
check("payment in allocates a partial amount to a receipt", code == 200
      and partial_preview["total"] == 20 and partial_preview["count"] == 1
      and partial_preview["items"][0]["amount"] == 20
      and not partial_preview["errors"], partial_preview)

code, allocation_preview = call("POST", "/api/payments/preview", token=temple_t, body={
    "user_id": zone_user_id, "amount": 20,
})
check("payment in allocates an amount without receipt numbers", code == 200
      and allocation_preview["total"] == 20 and allocation_preview["count"] >= 1
      and not allocation_preview["errors"], allocation_preview)

code, over_remit = call("POST", "/api/payments", token=temple_t, body={
    "user_id": zone_user_id, "receipt_numbers": [credit["invoice_number"]], "amount": 60,
})
check("payment in rejects handing over more cash than collected", code == 400, over_remit)

code, first_handover = call("POST", "/api/payments", token=temple_t, body={
    "user_id": zone_user_id, "receipt_numbers": [credit["invoice_number"]], "amount": 20,
})
check("first partial cash handover is recorded", code == 200
      and first_handover["amount"] == 20
      and first_handover["items"][0]["amount"] == 20
      and first_handover["items"][0]["amount_paid"] == 50
      and first_handover["items"][0]["balance_due"] == credit["total"] - 50, first_handover)

_, pending_partial = call("GET", f"/api/payments/pending?user_id={zone_user_id}", token=temple_t)
credit_remaining = next((row for row in pending_partial if row["invoice_number"] == credit["invoice_number"]), {})
check("partially handed-over cash stays remittable", credit_remaining.get("remittable") == 30
      and credit_remaining.get("cash_remitted") == 20, credit_remaining)

code, second_handover = call("POST", "/api/payments", token=temple_t, body={
    "user_id": zone_user_id, "receipt_numbers": [credit["invoice_number"]], "amount": 30,
})
check("second partial cash handover is recorded", code == 200
      and second_handover["amount"] == 30 and second_handover["items"][0]["amount"] == 30, second_handover)

_, pending_done = call("GET", f"/api/payments/pending?user_id={zone_user_id}", token=temple_t)
check("fully handed-over cash leaves the pending list",
      not any(row["invoice_number"] == credit["invoice_number"] for row in pending_done), pending_done)

code, credit_handover_detail = call("GET", f"/api/payments/{second_handover['id']}", token=temple_t)
check("cash receipt shows the invoice received and amount due", code == 200
      and credit_handover_detail["items"][0]["amount_paid"] == 50
      and credit_handover_detail["items"][0]["balance_due"] == credit["total"] - 50,
      credit_handover_detail)

# --- temple tally keeps online money separate from pending cash ---
code, tally_before = call("GET", "/api/payments/pending-summary", token=temple_t)
zone_before_tally = next((z for z in tally_before if z["zone_id"] == 2), {})
code, upi_tally = call("POST", "/api/pos/invoices", token=zone_t, body={
    "customer_name": "Online Tally", "customer_phone": "9999999999",
    "payment_mode": "upi", "items": [{"barcode": credit_book["barcode"], "qty": 1}],
})
check("zone UPI bill records online money", code == 200
      and upi_tally["status"] == "paid", upi_tally)
upi_tally_total = round(float(upi_tally.get("total") or 0), 2)

code, dist_upi = call("POST", "/api/pos/invoices", token=zone_t, body={
    "customer_name": "Distributor Online", "customer_phone": "9999999999",
    "payment_mode": "upi", "distributor_id": dist_me["user"]["id"],
    "items": [{"barcode": credit_book["barcode"], "qty": 1}],
})
check("distributor online bill settles at billing instead of being due to the zone",
      code == 200 and dist_upi["status"] == "paid"
      and dist_upi["amount_paid"] == dist_upi["total"]
      and dist_upi["balance_due"] == 0, dist_upi)
dist_upi_total = round(float(dist_upi.get("total") or 0), 2)
_, dist_upi_detail = call("GET", f"/api/pos/invoices/{dist_upi['invoice_id']}", token=super_t)
check("distributor online bill records a non-cash payment", code == 200
      and [(p["amount"], p["method"]) for p in dist_upi_detail["payments"]]
      == [(dist_upi_total, "upi")], dist_upi_detail)

_, tally_with_upi = call("GET", "/api/payments/pending-summary", token=temple_t)
zone_with_upi = next((z for z in tally_with_upi if z["zone_id"] == 2), {})
check("temple tally keeps online money separate from pending cash", code == 200
      and zone_with_upi.get("online_received")
      == round(zone_before_tally.get("online_received", 0) + upi_tally_total + dist_upi_total, 2)
      and zone_with_upi.get("cash_collected") == zone_before_tally.get("cash_collected")
      and zone_with_upi.get("amount") == zone_before_tally.get("amount"), zone_with_upi)
check("temple tally adds online money to the zone total", code == 200
      and zone_with_upi.get("total_collected")
      == round(zone_with_upi.get("cash_collected", 0) + zone_with_upi.get("online_received", 0), 2),
      zone_with_upi)

code, _ = call("DELETE", f"/api/pos/invoices/{upi_tally['invoice_id']}", token=super_t)
check("online tally bill can be removed", code == 200)
code, _ = call("DELETE", f"/api/pos/invoices/{dist_upi['invoice_id']}", token=super_t)
check("distributor online bill can be removed", code == 200)
_, tally_after_upi = call("GET", "/api/payments/pending-summary", token=temple_t)
zone_after_upi = next((z for z in tally_after_upi if z["zone_id"] == 2), {})
check("temple tally drops the online amount after the bill is removed", code == 200
      and zone_after_upi.get("online_received") == zone_before_tally.get("online_received")
      and zone_after_upi.get("total_collected") == zone_before_tally.get("total_collected"),
      zone_after_upi)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)