# ISKCON Jamnagar — Grocery & Inventory (AppSheet + Google Sheets)

No-code grocery purchase workflow and (later) inventory system.

- **Stage 1 (this package):** Request → Kitchen Manager Approval → Purchase → Receive → Check → Complete
- **Stage 2 (later):** Opening Stock → Purchases → Consumption/Issue → Current Stock → Low Stock Alerts

## What's in this folder

| File | Purpose |
| --- | --- |
| `schema.md` | Full Google Sheets table design (columns, keys, dropdowns) |
| `workflow.md` | Roles, statuses and the Stage 1 workflow in detail |
| `appsheet-config.md` | Exact AppSheet settings: tables, refs, security, views, actions, notifications |
| `apps-script.gs` | Optional Google Apps Script for Stage 2 stock posting + low-stock email |
| `data/*.csv` | Starter data + empty workflow templates (import directly into Sheets) |

## Build steps (45–60 minutes)

1. **Create the workbook.** Make a new Google Sheets file named
   `ISKCON Jamnagar - Grocery & Inventory`.
2. **Create one tab per table.** Tabs must be named exactly: `Users`, `Departments`,
   `Suppliers`, `Items`, `Grocery_Requests`, `Grocery_Request_Items`, `Purchases`,
   `Purchase_Items`, `Receiving`, `Receiving_Items`, `Settings`, `Dashboard`.
3. **Import starter data.** For each tab: `File > Import > Upload`, pick the matching
   CSV from `data/`, choose **Insert new sheet**, type the first header cell, and
   replace the default tab (or rename it) to the exact name above. For prefix, the
   CSV header row is already correct — the app reads "Column names in first row".
4. **Create the AppSheet app.** Go to `appsheet.com`, sign in with the Google account
   that owns the workbook, **Create > Start with existing data** and select the workbook.
   AppSheet will auto-detect the tables.
5. **Configure tables.** For every table, verify the key column and column types, then
   set refs exactly as described in `appsheet-config.md`. Set initial values for keys.
6. **Set security.** Under **Security > Security Filters** apply the filters in
   `appsheet-config.md` so staff only see their own requests.
7. **Add views and actions.** Build the views and action buttons described in
   `appsheet-config.md` (Submit, Approve, Reject, Receive, Check, Complete).
8. **Enable notifications.** Create the "Profile" / email notifications so the Kitchen
   Manager is emailed on submit and requesters are emailed on approve/receive/complete.
9. **Deploy.** Use **Set Security Mode**, then **Manage > Deploy** and share the app/
   install it from the app stores for the team.

## Key contact

- **Kitchen Manager / Admin:** `iskconjamnagarmandir@gmail.com`
  (this email must exist in the `Users` table with role `KitchenManager`).

## One important rule

The login email of each AppSheet user **must exactly match** that user's `Email`
value in the `Users` sheet — everything (ownership, approvals, notifications) hangs
off that match.
