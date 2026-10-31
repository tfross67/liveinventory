#!/usr/bin/env python3
"""Add (or reactivate) a propagator access code.

    python3 scripts/add_propagator.py "Gustavo"
    python3 scripts/add_propagator.py "Gustavo" --code some-existing-code
    python3 scripts/add_propagator.py "Gustavo" --disable

Prints the plaintext code once. Only its SHA-256 is stored, so a lost code
means running this again to mint a new one.
"""
import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys

PROJECT = "ruwyfesblmaurfuiaofw"
TOKEN = open(os.path.expanduser("~/.supabase/access-token")).read().strip()

WORDS = [
    "cedar", "willow", "sage", "olive", "juniper", "hazel", "aspen", "alder",
    "maple", "laurel", "myrtle", "pine", "cypress", "sorrel", "fern", "moss",
]


def run_sql(sql):
    r = subprocess.run(
        ["curl", "-sS", "-X", "POST",
         f"https://api.supabase.com/v1/projects/{PROJECT}/database/query",
         "-H", f"Authorization: Bearer {TOKEN}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"query": sql})],
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


def new_code():
    return f"{secrets.choice(WORDS)}-{secrets.randbelow(9000) + 1000}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--code", help="use this code instead of generating one")
    ap.add_argument("--disable", action="store_true", help="deactivate and print nothing")
    args = ap.parse_args()

    name = args.name.strip().replace("'", "''")
    if args.disable:
        run_sql(f"update public.propagators set active = false where name = '{name}';")
        print(f"disabled {args.name}")
        return

    code = (args.code or new_code()).strip().lower()
    digest = hashlib.sha256(code.encode()).hexdigest()
    run_sql(
        "insert into public.propagators (name, code_hash, active) "
        f"values ('{name}', '{digest}', true) "
        "on conflict (name) do update set code_hash = excluded.code_hash, active = true "
        "returning name;"
    )
    print(f"propagator: {args.name}")
    print(f"code:       {code}")


if __name__ == "__main__":
    main()
