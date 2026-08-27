#!/usr/bin/env python3
"""
Step 2 of the job-watch pipeline.

For every employer in seed_employers.csv, work out whether they run an
applicant tracking system with a public job feed, and record the endpoint.

This is the expensive step, but it is expensive in GitHub Actions minutes, not
in Claude tokens, and it is incremental: once an employer is resolved it is
never probed again. Unresolved employers are retried every RETRY_DAYS days in
case they change system.

Writes ats_registry.json:
  {"employers": [{"name":..., "ats":"greenhouse", "slug":"coursera",
                  "endpoint":"https://...", "checked":"2026-08-27"}],
   "unresolved": {"Some Ltd": "2026-08-27"}}
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

SEED = Path("seed_employers.csv")
REGISTRY = Path("ats_registry.json")
RETRY_DAYS = 30
WORKERS = 16
TIMEOUT = 8
UA = {"User-Agent": "rj-jobwatch/1.0 (personal job search; contact via repo)"}

# Words dropped when guessing the "short brand" form of a name.
STOP = re.compile(
    r"\b(limited|ltd|llp|plc|uk|gb|group|holdings|international|the|and|of"
    r"|services|solutions|company|co|inc|trading|as|t/a)\b",
    re.I,
)

# Legal suffixes only. Kept separate because "Study Group" and "Kaplan
# International" are part of the brand, but "Limited" never is.
LEGAL = re.compile(r"\b(limited|ltd|llp|plc|inc|co|company|t/a|trading as)\b", re.I)


def slugs(name: str) -> list[str]:
    """Candidate tenant slugs for an employer name, most likely first."""
    base = re.sub(r"[^\w\s&-]", " ", name)
    base = re.sub(r"&", " and ", base)
    words = [w for w in base.split() if w]
    trimmed = [w for w in words if not LEGAL.fullmatch(w)] or words
    core = [w for w in words if not STOP.fullmatch(w)] or words
    out: list[str] = []

    def add(s: str) -> None:
        s = re.sub(r"[^a-z0-9-]", "", s.lower())
        if 2 < len(s) < 40 and s not in out:
            out.append(s)

    # Brand-with-legal-suffix-stripped first: catches "studygroup",
    # "kaplaninternational", "oxfordinternational".
    add("".join(trimmed))
    add("-".join(trimmed))
    add("".join(trimmed[:2]))
    add("-".join(trimmed[:2]))
    # Then the aggressive short forms: catches "coursera", "multiverse".
    add("".join(core))
    add("-".join(core))
    add("".join(core[:2]))
    add(core[0])
    add(words[0])
    return out[:9]


# Each entry: (name, url template, predicate that says "this is a real board")
PATTERNS = [
    (
        "greenhouse",
        "https://boards-api.greenhouse.io/v1/boards/{s}/jobs",
        lambda d: isinstance(d, dict) and isinstance(d.get("jobs"), list) and len(d["jobs"]) > 0,
    ),
    (
        "lever",
        "https://api.lever.co/v0/postings/{s}?mode=json",
        lambda d: isinstance(d, list) and len(d) > 0,
    ),
    (
        "ashby",
        "https://api.ashbyhq.com/posting-api/job-board/{s}",
        lambda d: isinstance(d, dict) and isinstance(d.get("jobs"), list) and len(d["jobs"]) > 0,
    ),
    (
        "smartrecruiters",
        "https://api.smartrecruiters.com/v1/companies/{s}/postings",
        lambda d: isinstance(d, dict) and d.get("totalFound", 0) > 0,
    ),
    (
        "recruitee",
        "https://{s}.recruitee.com/api/offers/",
        lambda d: isinstance(d, dict) and isinstance(d.get("offers"), list) and len(d["offers"]) > 0,
    ),
    (
        "teamtailor",
        "https://{s}.teamtailor.com/jobs.json",
        lambda d: isinstance(d, (list, dict)) and bool(d),
    ),
    (
        "pinpoint",
        "https://{s}.pinpointhq.com/postings.json",
        lambda d: isinstance(d, (list, dict)) and bool(d),
    ),
]


def load() -> dict:
    if REGISTRY.exists():
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    return {"employers": [], "unresolved": {}}


def probe_one(session: requests.Session, name: str) -> dict | None:
    for slug in slugs(name):
        for ats, tmpl, ok in PATTERNS:
            url = tmpl.format(s=slug)
            try:
                r = session.get(url, timeout=TIMEOUT, headers=UA)
            except requests.RequestException:
                continue
            if r.status_code != 200:
                continue
            try:
                data = r.json()
            except ValueError:
                continue
            try:
                good = ok(data)
            except Exception:
                good = False
            if good:
                return {
                    "name": name,
                    "ats": ats,
                    "slug": slug,
                    "endpoint": url,
                    "checked": date.today().isoformat(),
                }
            time.sleep(0.05)
    return None


def main() -> int:
    if not SEED.exists():
        print("seed_employers.csv missing; run build_seed.py first", file=sys.stderr)
        return 1

    reg = load()
    resolved = {e["name"] for e in reg["employers"]}
    cutoff = (datetime.now() - timedelta(days=RETRY_DAYS)).date().isoformat()

    todo: list[str] = []
    with SEED.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            n = row["name"]
            if n in resolved:
                continue
            last = reg["unresolved"].get(n)
            if last and last > cutoff:
                continue
            todo.append(n)

    # Keep each Action run inside a sane wall-clock budget.
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    todo = todo[:limit]
    print(f"probing {len(todo)} employers ({len(resolved)} already resolved)")

    session = requests.Session()
    found = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for name, hit in zip(todo, pool.map(lambda n: probe_one(session, n), todo)):
            if hit:
                reg["employers"].append(hit)
                reg["unresolved"].pop(name, None)
                found += 1
                print(f"  + {name} -> {hit['ats']}/{hit['slug']}")
            else:
                reg["unresolved"][name] = date.today().isoformat()

    reg["employers"].sort(key=lambda e: e["name"].lower())
    REGISTRY.write_text(json.dumps(reg, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"resolved {found} new; registry now {len(reg['employers'])} employers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
