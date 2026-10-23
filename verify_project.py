import csv, sqlite3, urllib.request, json, sys
from pathlib import Path
base=Path(__file__).resolve().parent
with open(base/"data"/"linuxmate_dataset_300.csv",newline="",encoding="utf-8") as f: rows=list(csv.DictReader(f))
print("CSV dataset rows:",len(rows))
print("Unique topics:",len(set(r["topic"] for r in rows)))
print("Required columns:",", ".join(rows[0].keys()) if rows else "NONE")
try:
 print("Health endpoint:",urllib.request.urlopen("http://127.0.0.1:5000/health",timeout=2).read().decode())
except Exception as e: print("App health endpoint unavailable; start app.py first.",e)
if len(rows)!=300: sys.exit(1)
