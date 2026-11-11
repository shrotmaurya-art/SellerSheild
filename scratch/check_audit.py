import sqlite3
con = sqlite3.connect('agent/data/agent.db')
cur = con.cursor()
for row in cur.execute("SELECT event_type, action, outcome, details_json FROM audit_events ORDER BY seq DESC LIMIT 25"):
    print(f"[{row[0]}] {row[1]} -> {row[2]}")
    if row[3]:
        print("  details:", row[3][:160])
