#!/usr/bin/env python3
"""Fold production rows entered on enter.html into the four source CSVs.

    python3 scripts/reconcile_entries.py            # dry run: show what would change
    python3 scripts/reconcile_entries.py --apply    # write CSVs + mark rows reconciled

Rows come from public.production_entries where reconciled = false (the same
rows the tracker renders with a "new" chip). For each row we append a line to
the CSV belonging to its tab, keep the data block sorted by production date
ascending (the files' existing order), skip exact duplicates, and then mark the
rows reconciled so the tracker stops showing them as pending.

Line endings are preserved per file: liners/4inch/1gallon are CRLF, 5gallon LF.
Header lines (the title row and the PLANT NAME column header) are left untouched.
"""
import argparse
import csv
import io
import json
import os
import subprocess
import sys
from datetime import date

PROJECT = "ruwyfesblmaurfuiaofw"
TOKEN = open(os.path.expanduser("~/.supabase/access-token")).read().strip()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CSV_FOR_TAB = {
    "liners": "liners.csv",
    "4inch": "4inch.csv",
    "1gallon": "1gallon.csv",
    "5gallon": "5gallon.csv",
}


def sql(query):
    r = subprocess.run(
        ["curl", "-sS", "-X", "POST",
         f"https://api.supabase.com/v1/projects/{PROJECT}/database/query",
         "-H", f"Authorization: Bearer {TOKEN}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"query": query})],
        capture_output=True, text=True, timeout=90,
    )
    if r.returncode != 0:
        sys.exit(f"curl failed: {r.stderr}")
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        sys.exit(f"bad response: {r.stdout[:400]}")
    if isinstance(out, dict) and out.get("message"):
        sys.exit(f"api error: {out['message']}")
    return out


def us_date(iso):
    y, m, d = iso.split("-")
    return f"{int(m)}/{int(d)}/{y}"


def sort_key(line):
    """Production date of a CSV data line (col 2), tolerant of quoting."""
    try:
        row = next(csv.reader(io.StringIO(line)))
        m, d, y = row[1].split("/")
        return date(int(y), int(m), int(d))
    except Exception:
        return date(1970, 1, 1)


def csv_line(row):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(row)
    return buf.getvalue()


def split_file(path):
    """Return (prefix_lines, data_lines, newline) — prefix keeps title + header."""
    with open(path, "r", newline="", encoding="utf-8") as f:
        raw = f.readlines()
    newline = "\r\n" if raw and raw[0].endswith("\r\n") else "\n"
    header_idx = next(
        (i for i, ln in enumerate(raw) if ln.strip().upper().startswith("PLANT NAME")),
        None,
    )
    if header_idx is None:
        sys.exit(f"{path}: no 'PLANT NAME' header row found")
    prefix = [ln.rstrip("\r\n") for ln in raw[: header_idx + 1]]
    data = [ln.rstrip("\r\n") for ln in raw[header_idx + 1 :] if ln.strip()]
    return prefix, data, newline


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the CSVs and mark rows reconciled")
    args = ap.parse_args()

    entries = sql(
        "select id, tab, plant_name, row_size, production_date::text as production_date, "
        "qty, source, notes from public.production_entries "
        "where reconciled = false order by production_date, created_at;"
    )
    if not entries:
        print("No unreconciled entries. Nothing to do.")
        return

    by_tab = {}
    for e in entries:
        by_tab.setdefault(e["tab"], []).append(e)

    applied_ids, summary = [], []
    for tab, rows in by_tab.items():
        fname = CSV_FOR_TAB.get(tab)
        if not fname:
            sys.exit(f"unknown tab: {tab}")
        path = os.path.join(REPO, fname)
        prefix, data, newline = split_file(path)
        existing = set(data)

        # Insert in place by production date so untouched lines keep their
        # current order (these files are date-ascending but not perfectly
        # sorted; a full re-sort would produce diff noise on every run).
        keyed = [(sort_key(ln), ln) for ln in data]
        added = []
        for e in rows:
            line = csv_line([
                e["plant_name"],
                us_date(e["production_date"]),
                e["qty"],
                e["row_size"] or "",
                e["source"] or "",
                e["notes"] or "",
            ])
            if line in existing:
                print(f"  duplicate, skipping: {fname} :: {e['plant_name']} {us_date(e['production_date'])}")
                applied_ids.append(e["id"])
                continue
            key = sort_key(line)
            pos = next((i for i, (k, _) in enumerate(keyed) if k > key), len(keyed))
            keyed.insert(pos, (key, line))
            existing.add(line)
            added.append(line)
            applied_ids.append(e["id"])

        data = [ln for _, ln in keyed]
        summary.append((fname, len(added), len(rows)))

        if args.apply and added:
            with open(path, "w", newline="", encoding="utf-8") as f:
                f.write(newline.join(prefix) + newline)
                f.write(newline.join(data) + newline)
            print(f"  wrote {fname}: +{len(added)} row(s)")

    print(f"\n{len(applied_ids)} entr(y/ies) across {len(by_tab)} tab(s) "
          f"({'applied' if args.apply else 'dry run — rerun with --apply'})")
    for fname, n_added, n_seen in summary:
        print(f"  {fname}: {n_added} added / {n_seen} pending")

    if not args.apply:
        return

    ids = ",".join(f"'{i}'" for i in applied_ids)
    out = sql(f"select public.mark_entries_reconciled(array[{ids}]::uuid[]) as marked;")
    print(f"marked reconciled: {out}")


if __name__ == "__main__":
    main()
