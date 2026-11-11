import sqlite3, json
con = sqlite3.connect('agent/data/agent.db')
cur = con.cursor()
for row in cur.execute("SELECT session_id, role, text, cards_json FROM messages ORDER BY id DESC LIMIT 15"):
    print(f"[{row[1]}] {row[2][:80] if row[2] else ''}")
    if row[3]:
        print("  cards:", row[3][:120])

