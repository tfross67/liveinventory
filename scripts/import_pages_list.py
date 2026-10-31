#!/usr/bin/env python3
"""Import a propagator's weekly list (Pages doc or plain text) into the portal.

The list she writes looks like this:

    1 Gallon

    Week of October 12th, 2026

    500 Lavandula 'Anouk Supreme'
    500 Phlomis purpurea
    ...
    Total: 7,140

    For 4 inch this week:

    892 Phyla nodiflora (white)
    ...
    Total: 2,538

Usage:
    python3 scripts/import_pages_list.py Gallons.pages             # review only
    python3 scripts/import_pages_list.py Gallons.pages --apply     # submit
    python3 scripts/import_pages_list.py list.txt --apply --code cedar-1234

--apply needs an access code (--code, or NS_PROP_CODE in the env). Rows are
posted through the prop-entry Edge Function, same as the browser form.

The size heading decides the tab; the "Week of ..." line supplies the
production date. Anything that can't be matched to the plant catalog is
reported and skipped rather than guessed at.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FN_URL = "https://ruwyfesblmaurfuiaofw.supabase.co/functions/v1/prop-entry"

TAB_SIZES = {"4inch": "4inch", "1gallon": "1gal", "5gallon": "5gal"}
CATALOG_SIZE_FOR_TAB = {"4inch": "4inch", "1gallon": "1gal", "5gallon": "5gal", "liners": None}
ROW_SIZE_FOR_TAB = {"4inch": '4" 16 Tray', "1gallon": "1 gallon", "5gallon": "5g", "liners": ""}

HEADING_RE = re.compile(
    r"^\s*(?:For\s+)?(\d+)\s*(?:\"|inch|in|gallon|gal)\b.*?(:|$)", re.I
)
WEEK_RE = re.compile(r"Week of ([A-Za-z]+ \d{1,2}(?:st|nd|rd|th)?, \d{4})", re.I)
LINE_RE = re.compile(r"^\s*(\d[\d,]*)\s+(.+?)\s*$")
TOTAL_RE = re.compile(r"^\s*Total:\s*([\d,]+)")


# ---------- catalog ----------
def load_catalog():
    path = os.path.join(REPO, "plant-catalog.js")
    raw = open(path, encoding="utf-8").read()
    raw = re.sub(r"^.*?window\.PLANT_CATALOG\s*=\s*", "", raw, flags=re.S)
    raw = raw.rstrip().rstrip(";")
    return json.loads(raw)


def norm(s: str) -> str:
    s = s.replace("\u2018", "'").replace("\u2019", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.lower().strip()
    s = re.sub(r"\s*\((new|patented|ppaf|pp\d+[^)]*)\)\s*", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .,")


def balance_quotes(s: str) -> str:
    """Pages drops the closing curly quote on run boundaries; put it back.

    Normalise the curly forms first, otherwise an odd number of U+2018 openers
    looks like zero quote characters and the cultivar is never seen.
    """
    s = s.replace("\u2018", "'").replace("\u2019", "'")
    if s.count("'") % 2 == 1:
        s = s + "'"
    return s


# Her list uses a few common names where the catalog uses the botanical genus.
COMMON_NAME_ALIASES = {
    "thyme": "thymus",
    "lavender": "lavandula",
    "rosemary": "rosmarinus",
    "sage": "salvia",
    "penstemon": "penstemon",
}


def genus_of(s: str):
    g = norm(s).split(" ")[0]
    return COMMON_NAME_ALIASES.get(g, g)


def cultivar_of(s: str):
    """Quoted cultivar, greedy to the closing quote.

    Non-greedy matching stops at the apostrophe in a possessive cultivar
    ("Juncus 'Carman's Grey'" would yield just 'carman'), which then collides
    with every other Carman cultivar.
    """
    m = re.search(r"'(.+)'", s)
    return norm(m.group(1)) if m else None


def build_matcher(catalog):
    """Index the catalog by normalized name, merging duplicate names.

    The catalog carries the same plant twice under different patent suffixes
    ("Epilobium 'Bowman's #1'" and "... (PPAF)"), which normalize to one name.
    Left unmerged they look like an ambiguous match; merging their size lists
    makes them a single plant with several sizes.
    """
    merged = {}
    for plant in catalog:
        n = norm(plant["n"])
        if n in merged:
            have = {(s, c) for s, c in merged[n]["i"]}
            for pair in plant["i"]:
                if tuple(pair) not in have:
                    merged[n]["i"].append(pair)
                    have.add(tuple(pair))
        else:
            merged[n] = {"n": plant["n"], "i": [list(p) for p in plant["i"]]}

    by_genus = {}
    for plant in merged.values():
        by_genus.setdefault(genus_of(plant["n"]), []).append(plant)
    return merged, by_genus


def match(name, tab, exact, by_genus):
    """Return (plant, how) or (None, reason)."""
    target = norm(balance_quotes(name))
    if target in exact:
        return exact[target], "exact"

    genus = genus_of(target)
    cultivar = cultivar_of(target)
    candidates = by_genus.get(genus, [])
    want = CATALOG_SIZE_FOR_TAB[tab]

    def has_size(p):
        return not want or any(s == want for s, _ in p["i"])

    if cultivar:
        hits = [p for p in candidates if cultivar_of(norm(p["n"])) == cultivar]
        sized = [p for p in hits if has_size(p)]
        pool = sized or hits
        if len(pool) == 1:
            return pool[0], "cultivar" if pool is sized else "cultivar (no {0} size)".format(want)
        if len(pool) > 1:
            # several names share the cultivar — pick the one closest to what she wrote
            toks = set(target.split())
            best = sorted(pool, key=lambda p: -len(toks & set(norm(p["n"]).split())))
            scored = [(len(toks & set(norm(p["n"]).split())), p) for p in best]
            if scored[0][0] > scored[1][0]:
                return scored[0][1], "cultivar+similarity"
            return None, "ambiguous cultivar ({0} matches)".format(len(pool))

    # Every word she wrote appears in a catalog name in this genus
    # ("Buddleja Buzz 'Purple'" -> "Buddleja 'Buzz Pink Purple'").
    toks = [t for t in target.replace("'", " ").split() if t not in ("x", "var.")]
    contained = [
        p for p in candidates if all(t in norm(p["n"]).replace("'", " ") for t in toks)
    ]
    sized = [p for p in contained if has_size(p)]
    pool = sized or contained
    if len(pool) == 1:
        return pool[0], "all words contained"

    pref = [p for p in candidates if norm(p["n"]).startswith(target)]
    if len(pref) == 1:
        return pref[0], "prefix"
    if len(pref) > 1:
        sized = [p for p in pref if has_size(p)]
        if len(sized) == 1:
            return sized[0], "prefix+size"
        return None, "ambiguous prefix ({0} matches)".format(len(pref))

    tokset = set(target.split())
    scored = []
    for p in candidates:
        pt = set(norm(p["n"]).split())
        score = len(tokset & pt) / max(len(tokset), 1)
        if score >= 0.6:
            scored.append((score, p))
    if len(scored) == 1:
        return scored[0][1], "similar"
    if len(scored) > 1:
        scored.sort(key=lambda x: -x[0])
        if scored[0][0] > scored[1][0]:
            return scored[0][1], "similar"
        return None, "ambiguous ({0} similar)".format(len(scored))

    return None, "no catalog match"


# ---------- text + parse ----------
def read_text(path: str) -> str:
    if path.lower().endswith(".pages"):
        sys.path.insert(0, os.path.join(REPO, "scripts"))
        import pages_text

        payload = pages_text.document_payload(path)
        return pages_text.trim_to_body(pages_text.reconstruct(pages_text.extract_runs(payload)))
    return open(path, encoding="utf-8").read()


def heading_tab(line: str):
    """'1 Gallon' / 'For 4 inch this week:' -> tab. None when it is a plant line.

    '1 Gallon' also matches the '<qty> <name>' shape (qty 1, name 'Gallon'), so
    the size word has to be recognised before the line is treated as data.
    """
    m = re.match(r'^(?:For\s+)?(\d+)\s*(?:"|inch\b|in\b|gallon\b|gal\b)(.*)$', line, re.I)
    if not m:
        return None
    rest = m.group(2).strip(" :.")
    if line.lower().startswith("for ") or rest == "":
        return {1: "1gallon", 4: "4inch", 5: "5gallon"}.get(int(m.group(1)))
    return None


def parse(text: str):
    week = None
    tab = None
    rows = []
    totals = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        m = WEEK_RE.search(line)
        if m:
            week = m.group(1)
            continue

        if TOTAL_RE.match(line) and tab:
            totals[tab] = int(TOTAL_RE.match(line).group(1).replace(",", ""))
            continue

        h = heading_tab(line)
        if h:
            tab = h
            continue

        if norm(line) in ("liners", "liner"):
            tab = "liners"
            continue

        lm = LINE_RE.match(line)
        if lm and tab:
            rows.append({"qty": int(lm.group(1).replace(",", "")), "name": lm.group(2).strip(), "tab": tab})

    return week, rows, totals


def parse_week_date(week: str):
    if not week:
        return None
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", week)
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--skip-unmatched", action="store_true",
                    help="submit the matched rows even though some lines could not be matched")
    ap.add_argument("--code", default=os.environ.get("NS_PROP_CODE", ""))
    args = ap.parse_args()

    text = read_text(args.path)
    week, rows, totals = parse(text)
    if not rows:
        sys.exit("No '<qty> <plant>' lines found — is this the right document?")

    date = parse_week_date(week)
    if not date:
        sys.exit(f"Could not read a 'Week of ...' date (got {week!r}); refusing to guess a production date.")

    catalog = load_catalog()
    exact, by_genus = build_matcher(catalog)

    matched, unmatched = [], []
    for r in rows:
        plant, how = match(r["name"], r["tab"], exact, by_genus)
        if not plant:
            unmatched.append((r, how))
            continue
        size = CATALOG_SIZE_FOR_TAB[r["tab"]]
        code = None
        if size:
            for s, c in plant["i"]:
                if s == size:
                    code = c
                    break
        matched.append({
            "tab": r["tab"],
            "plant_name": plant["n"],
            "item_code": code,
            "catalog_size": size,
            "row_size": ROW_SIZE_FOR_TAB[r["tab"]],
            "production_date": date.isoformat(),
            "qty": r["qty"],
            "source": "",
            "notes": "",
            "_written": r["name"],
            "_how": how,
            "_size_missing": bool(size) and code is None,
        })

    # ---- report ----
    print(f"week:  {week}  ->  production date {date.isoformat()}")
    print(f"rows:  {len(rows)}  matched {len(matched)}  unmatched {len(unmatched)}\n")
    for m in matched:
        flag = "  !! no {0} item code".format(m["catalog_size"]) if m["_size_missing"] else ""
        print(f"  {m['qty']:>6}  {m['tab']:<7} {m['plant_name']}{flag}")
    if unmatched:
        print("\nUNMATCHED (skipped — add them by hand or fix the name):")
        for r, why in unmatched:
            print(f"  {r['qty']:>6}  {r['tab']:<7} {r['name']}   [{why}]")

    print("\ntotals check:")
    for tab, expected in totals.items():
        got = sum(m["qty"] for m in matched if m["tab"] == tab) + sum(
            r["qty"] for r, _ in unmatched if r["tab"] == tab
        )
        ok = "OK" if got == expected else f"MISMATCH (parsed {got})"
        print(f"  {tab:<8} document {expected:>7}   {ok}")

    if not args.apply:
        print("\nreview only — rerun with --apply to submit")
        return

    if unmatched and not args.skip_unmatched:
        sys.exit("\nrefusing to submit while rows are unmatched; resolve them first "
                 "(or pass --skip-unmatched to submit the rest)")
    if not args.code:
        sys.exit("\n--apply needs --code (or NS_PROP_CODE in the environment)")

    payload = [{k: v for k, v in m.items() if not k.startswith("_")} for m in matched]
    req = urllib.request.Request(
        FN_URL,
        data=json.dumps({"action": "submit", "code": args.code, "rows": payload}).encode(),
        headers={"Content-Type": "application/json", "Origin": "https://tfross67.github.io"},
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except urllib.error.HTTPError as e:
        sys.exit(f"submit failed ({e.code}): {e.read().decode()[:400]}")
    print(f"\nsubmitted: {resp.get('inserted')} rows")


if __name__ == "__main__":
    main()
