#!/usr/bin/env python3
"""Export the NS master plant catalog from Supabase into liveinventory's
plant-catalog.js. Compact shape: [{n: name, i: [[size, item_code], ...]}]."""
import json, os, subprocess, sys

PROJECT = "ruwyfesblmaurfuiaofw"
TOKEN = open(os.path.expanduser("~/.supabase/access-token")).read().strip()
OUT = "/Users/tfross/.hermes/liveinventory/plant-catalog.js"

SQL = """
select item_code, size, description
from public.master_items
where item_code is not null and description is not null and description <> ''
order by lower(description), size
"""

r = subprocess.run(
    ["curl", "-sS", "-X", "POST",
     f"https://api.supabase.com/v1/projects/{PROJECT}/database/query",
     "-H", f"Authorization: Bearer {TOKEN}",
     "-H", "Content-Type: application/json",
     "-d", json.dumps({"query": SQL})],
    capture_output=True, text=True, timeout=120,
)
if r.returncode != 0:
    sys.exit(f"curl failed: {r.stderr}")
try:
    rows = json.loads(r.stdout)
except json.JSONDecodeError:
    sys.exit(f"bad response: {r.stdout[:500]}")
if isinstance(rows, dict):
    sys.exit(f"api error: {json.dumps(rows)[:500]}")

by_name = {}
for row in rows:
    name = (row["description"] or "").strip()
    size = (row["size"] or "").strip()
    code = (row["item_code"] or "").strip()
    if not name or not code:
        continue
    by_name.setdefault(name, []).append([size, code])

items = []
for name in sorted(by_name, key=str.lower):
    entries = sorted(by_name[name], key=lambda x: x[0])
    # de-dup identical size/code pairs
    seen, clean = set(), []
    for size, code in entries:
        if (size, code) in seen:
            continue
        seen.add((size, code))
        clean.append([size, code])
    items.append({"n": name, "i": clean})

payload = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
header = (
    "// Auto-generated from Supabase public.master_items — do not hand-edit.\n"
    "// shape: {n: plant name, i: [[size, item_code], ...]}\n"
    "// Regenerate: ~/.hermes/liveinventory/scripts/build_plant_catalog.py\n"
)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(header)
    f.write("window.PLANT_CATALOG=" + payload + ";\n")

print(json.dumps({
    "rows_from_db": len(rows),
    "names": len(items),
    "codes": sum(len(i["i"]) for i in items),
    "bytes": os.path.getsize(OUT),
}))
