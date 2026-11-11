import sqlite3
con = sqlite3.connect('store-api/data/store.db')
cur = con.cursor()
for row in cur.execute("SELECT order_id, customer_id, status FROM orders WHERE customer_id='C001'"):
    print(row)
