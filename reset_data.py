import sqlite3
from pathlib import Path

db = Path('data/inventory.db')
conn = sqlite3.connect(db)
c = conn.cursor()

for tbl in ['audit_logs', 'invoice_items', 'invoices', 'damaged', 'stock_count_items', 'stock_counts', 'return_items', 'returns', 'transfer_items', 'transfers', 'stock_movements', 'stock']:
    c.execute('DELETE FROM ' + tbl)

c.execute('DELETE FROM books')

books = [
    ('Bhagavat Gita', None, 'BG001', None, None, 0),
    ('Ramayan', None, 'RM001', None, None, 0),
    ('Krishna', None, 'KR001', None, None, 0),
]
for b in books:
    c.execute("INSERT INTO books (title, isbn, barcode, language, publication, price) VALUES (?,?,?,?,?,?)", b)

conn.commit()
conn.close()
print('done')
