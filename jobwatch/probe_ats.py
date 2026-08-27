#!/usr/bin/env python3
"""
Step 2 of the job-watch pipeline.

For every employer in seed_employers.csv, work out whether they run an
applicant tracking system with a public job feed, and record the endpoint.

Expensive in GitHub Actions minutes, not in Claude tokens, and incremental:
once an employer is resolved it is never probed again. Unresolved employers
are retried every RETRY_DAYS days in case they change system.

v2, 2026-08-27. The first run resolved 293 employers of which 253 were wrong:
the slug generator fell back to a single generic word, so "Abacus Nursery
School" matched greenhouse/abacus, a completely unrelated company. A wrong
employer here silently breaks the sponsorship gate, which is worse than no
data at all. Two defences added:
  1. a slug must be built from at least two words of the employer name,
     unless the name really is one word
  2. where the ATS tells us the board's own company name, it must look like
     the employer we were searching for

Writes ats_registry.json:
  {"employers": [{"name":..., "ats":"greenhouse", "slug":"coursera",
                  "endpoint":"https://...", "confirmed_as":"Coursera",
                  "checked":"2026-08-27"}],
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

# Dropped when guessing the short brand form.
STOP = re.compile(
    r"^(limited|ltd|llp|plc|uk|gb|group|holdings|international|the|and|of|for"
    r"|services|solutions|company|co|inc|trading|as|t/a|cic|trust|academy"
    r"|academies|school|schools|college|university|centre|center)$",
    re.I,
)

# Legal suffixes only.
LEGAL = re.compile(r"^(limited|ltd|llp|plc|inc|co|company|t/a|cic)$", re.I)


def words_of(name: str) -> list[str]:
    base = re.sub(r"[^\w\s&-]", " ", name)
    base = re.sub(r"&", " and ", base)
    return [w for w in base.split() if w]


def slugs(name: str) -> list[str]:
    """Candidate tenant slugs, most likely first.

    Every candidate must carry at least two words of the employer name, so a
    generic single word can never match somebody else's board. The only
    exception is an employer whose name really is one distinctive word.
    """
    words = words_of(name)
    trimmed = [w for w in words if not LEGAL.fullmatch(w)] or words
    core = [w for w in trimmed if not STOP.fullmatch(w)] or trimmed

    out: list[str] = []

    def add(parts: list[str], joiner: str = "") -> None:
        if not parts:
            return
        s = re.sub(r"[^a-z0-9-]", "", joiner.join(parts).lower())
        if 3 < len(s) < 40 and s not in out:
            out.append(s)

    if len(trimmed) == 1:
        # Genuinely single-word employer: Jisc, Unibuddy, Coursera.
        add(trimmed)
        return out

    add(trimmed)
    add(trimmed, "-")
    add(trimmed[:2])
    add(trimmed[:2], "-")
    # Only use the stop-word-stripped form when it still carries two words.
    # A lone survivor like "adams" out of "Adams Academy Inc Ltd" would match
    # somebody else's board.
    if len(core) >= 2:
        add(core)
        add(core, "-")
        add(core[:2])
        add(core[:2], "-")
    return out[:8]


def alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def looks_like(employer: str, board_name: str) -> bool:
    """Does the board's own company name plausibly belong to this employer?"""
    if not board_name:
        return True  # nothing to check against; the slug rule already applied
    a, b = alnum(employer), alnum(board_name)
    if not a or not b:
        return True
    if a == b:
        return True
    # Containment only counts when the two names are a similar length.
    # "abacus" sits inside "abacushealth" but they are different companies.
    if (a in b or b in a) and min(len(a), len(b)) / max(len(a), len(b)) >= 0.6:
        return True
    ta = {w.lower() for w in words_of(employer) if not STOP.fullmatch(w) and len(w) > 2}
    tb = {w.lower() for w in words_of(board_name) if not STOP.fullmatch(w) and len(w) > 2}
    if not ta or not tb:
        return False
    # Jaccard, not min-overlap: min-overlap says "Adams" and "Adams Street
    # Partners" are a perfect match, which is how the first run went wrong.
    return len(ta & tb) / len(ta | tb) >= 0.45


# (name, jobs url template, predicate, board-name url template or None,
#  function pulling the company name out of the board-name response)
PATTERNS = [
    (
        "greenhouse",
        "https://boards-api.greenhouse.io/v1/boards/{s}/jobs",
        lambda d: isinstance(d, dict) and isinstance(d.get("jobs"), list) and len(d["jobs"]) > 0,
        "https://boards-api.greenhouse.io/v1/boards/{s}",
        lambda d: d.get("name", "") if isinstance(d, dict) else "",
    ),
    (
        "lever",
        "https://api.lever.co/v0/postings/{s}?mode=json",
        lambda d: isinstance(d, list) and len(d) > 0,
        None,
        None,
    ),
    (
        "ashby",
        "https://api.ashbyhq.com/posting-api/job-board/{s}",
        lambda d: isinstance(d, dict) and isinstance(d.get("jobs"), list) and len(d["jobs"]) > 0,
        None,
        None,
    ),
    (
        "smartrecruiters",
        "https://api.smartrecruiters.com/v1/companies/{s}/postings",
        lambda d: isinstance(d, dict) and d.get("totalFound", 0) > 0,
        None,
        None,
    ),
    (
        "recruitee",
        "https://{s}.recruitee.com/api/offers/",
        lambda d: isinstance(d, dict) and isinstance(d.get("offers"), list) and len(d["offers"]) > 0,
        None,
        None,
    ),
    (
        "teamtailor",
        "https://{s}.teamtailor.com/jobs.json",
        lambda d: isinstance(d, (list, dict)) and bool(d),
        None,
        None,
    ),
    (
        "pinpoint",
        "https://{s}.pinpointhq.com/postings.json",
        lambda d: isinstance(d, (list, dict)) and bool(d),
        None,
        None,
    ),
]


def load() -> dict:
    if REGISTRY.exists():
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    return {"employers": [], "unresolved": {}}


def probe_one(session: requests.Session, name: str) -> dict | None:
    for slug in slugs(name):
        for ats, tmpl, ok, name_tmpl, pick_name in PATTERNS:
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
            if not good:
                time.sleep(0.05)
                continue

            board_name = ""
            if name_tmpl:
                try:
                    rn = session.get(name_tmpl.format(s=slug), timeout=TIMEOUT, headers=UA)
                    if rn.status_code == 200:
                        board_name = pick_name(rn.json()) or ""
                except Exception:
                    board_name = ""

            if not looks_like(name, board_name):
                time.sleep(0.05)
                continue

            return {
                "name": name,
                "ats": ats,
                "slug": slug,
                "endpoint": url,
                "confirmed_as": board_name,
                "checked": date.today().isoformat(),
            }
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
                extra = f" (board says: {hit['confirmed_as']})" if hit["confirmed_as"] else ""
                print(f"  + {name} -> {hit['ats']}/{hit['slug']}{extra}")
            else:
                reg["unresolved"][name] = date.today().isoformat()

    reg["employers"].sort(key=lambda e: e["name"].lower())
    REGISTRY.write_text(json.dumps(reg, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"resolved {found} new; registry now {len(reg['employers'])} employers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
