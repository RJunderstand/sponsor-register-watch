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
data at all. Two defences were added: a slug had to carry two words of the
name, and the board's own company name had to look right.

v3, 2026-08-27. The two-word rule was too blunt and caused the opposite
failure: run 3 missed Coursera and D2L, both hand-verified as working.
"Coursera UK Limited" reduces to the single word "coursera", and the rule
threw that candidate away. The single-word candidate is now allowed back,
under three conditions that between them keep the Abacus hole shut:

  1. it is only built when exactly one distinctive word survives, so
     "Abacus Nursery School" (two survivors) can still never produce it
  2. the surviving word must not be a sector noun. "The Learning Company"
     reduces to "learning", which would match somebody else's board, so any
     word matching the education, technology or HE vocabulary is refused
  3. it is only tried against an ATS that reports its own company name, so
     the guess gets checked. The one exception is the tier 0 watchlist,
     whose names were verified by hand, where it is tried everywhere

Only Greenhouse publishes a board name. Ashby, Lever, SmartRecruiters,
Recruitee, Teamtailor and Pinpoint were all checked and do not, so for
tiers 1 to 3 a single-word guess is Greenhouse-only by construction.

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
# Bump whenever slugs() or looks_like() changes, so past failures are retried.
RULES_VERSION = 3
RETRY_DAYS = 30
WORKERS = 16
TIMEOUT = 8
UA = {"User-Agent": "rj-jobwatch/1.0 (personal job search; contact via repo)"}

# Dropped when guessing the short brand form. Legal suffixes, structural
# nouns and the geographies that get bolted onto a UK subsidiary's name:
# "Coursera UK Limited" and "D2L Europe Limited" are the brand plus noise.
STOP = re.compile(
    r"^(limited|ltd|llp|plc|uk|gb|group|holdings|international|the|and|of|for"
    r"|services|solutions|company|co|inc|trading|as|t/a|cic|trust|academy"
    r"|academies|school|schools|college|university|centre|center"
    r"|europe|emea|apac|global|worldwide|england|scotland|wales|ireland"
    r"|technologies|systems|partners|associates|enterprises|ventures)$",
    re.I,
)

# Legal suffixes only.
LEGAL = re.compile(r"^(limited|ltd|llp|plc|inc|co|company|t/a|cic)$", re.I)

# A lone survivor matching any of these is a sector noun, not a brand.
# "The Learning Company Limited" reduces to "learning"; that must not become
# a slug, because somebody else's board is certainly called that.
GENERIC = re.compile(
    r"^(universit\w*|college\w*|institut\w*|academ\w*|school\w*|educat\w*"
    r"|learn\w*|train\w*|teach\w*|tutor\w*|student\w*|skill\w*|curricul\w*"
    r"|technolog\w*|softwar\w*|digital\w*|data\w*|cloud\w*|cyber\w*"
    r"|analytic\w*|platform\w*|comput\w*|innovat\w*|lab\w*|studio\w*"
    r"|media|interactive|consult\w*|recruit\w*|health\w*|care|support"
    r"|research|science|sciences|global|national|british|london)$",
    re.I,
)


def words_of(name: str) -> list[str]:
    base = re.sub(r"[^\w\s&-]", " ", name)
    base = re.sub(r"&", " and ", base)
    return [w for w in base.split() if w]


def slugs(name: str) -> list[tuple[str, bool]]:
    """Candidate tenant slugs, most likely first.

    Returns (slug, is_single_word_guess). A single-word guess is riskier and
    the caller restricts where it may be tried.
    """
    words = words_of(name)
    trimmed = [w for w in words if not LEGAL.fullmatch(w)] or words
    core = [w for w in trimmed if not STOP.fullmatch(w)] or trimmed

    out: list[tuple[str, bool]] = []
    seen: set[str] = set()

    def add(parts: list[str], joiner: str = "", single: bool = False) -> None:
        if not parts:
            return
        s = re.sub(r"[^a-z0-9-]", "", joiner.join(parts).lower())
        if 2 < len(s) < 40 and s not in seen:
            seen.add(s)
            out.append((s, single))

    if len(trimmed) == 1:
        # Genuinely single-word employer: Jisc, Unibuddy, Kortext. The name
        # itself is the evidence, so this is not a guess.
        add(trimmed)
        return out

    add(trimmed)
    add(trimmed, "-")
    add(trimmed[:2])
    add(trimmed[:2], "-")

    if len(core) >= 2:
        add(core)
        add(core, "-")
        add(core[:2])
        add(core[:2], "-")
    elif len(core) == 1 and not GENERIC.fullmatch(core[0]) and len(core[0]) > 2:
        # Coursera, D2L, Multiverse, Bottomline. Marked as a guess.
        add(core, single=True)

    return out[:9]


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
    # Strip legal suffixes only, never STOP. STOP is tuned for building a
    # slug, where "Academy" and "Partners" are noise to be discarded. Here
    # they are the discriminating signal: throwing them away made "Adams
    # Academy" and "Adams Street Partners" a 0.5 match, which is the run 1
    # false positive coming straight back.
    ta = {w.lower() for w in words_of(employer) if not LEGAL.fullmatch(w) and len(w) > 2}
    tb = {w.lower() for w in words_of(board_name) if not LEGAL.fullmatch(w) and len(w) > 2}
    if not ta or not tb:
        return False
    # Jaccard, not min-overlap: min-overlap says "Adams" and "Adams Street
    # Partners" are a perfect match, which is how the first run went wrong.
    return len(ta & tb) / len(ta | tb) >= 0.45


# (name, jobs url, predicate, board-name url or None, company-name getter,
#  verifiable). Only Greenhouse publishes a board name; the other six were
# checked on 2026-08-27 and expose nothing that identifies the tenant.
PATTERNS = [
    (
        "greenhouse",
        "https://boards-api.greenhouse.io/v1/boards/{s}/jobs",
        lambda d: isinstance(d, dict) and isinstance(d.get("jobs"), list) and len(d["jobs"]) > 0,
        "https://boards-api.greenhouse.io/v1/boards/{s}",
        lambda d: d.get("name", "") if isinstance(d, dict) else "",
        True,
    ),
    (
        "lever",
        "https://api.lever.co/v0/postings/{s}?mode=json",
        lambda d: isinstance(d, list) and len(d) > 0,
        None,
        None,
        False,
    ),
    (
        "ashby",
        "https://api.ashbyhq.com/posting-api/job-board/{s}",
        lambda d: isinstance(d, dict) and isinstance(d.get("jobs"), list) and len(d["jobs"]) > 0,
        None,
        None,
        False,
    ),
    (
        "smartrecruiters",
        "https://api.smartrecruiters.com/v1/companies/{s}/postings",
        lambda d: isinstance(d, dict) and d.get("totalFound", 0) > 0,
        None,
        None,
        False,
    ),
    (
        "recruitee",
        "https://{s}.recruitee.com/api/offers/",
        lambda d: isinstance(d, dict) and isinstance(d.get("offers"), list) and len(d["offers"]) > 0,
        None,
        None,
        False,
    ),
    (
        "teamtailor",
        "https://{s}.teamtailor.com/jobs.json",
        lambda d: isinstance(d, (list, dict)) and bool(d),
        None,
        None,
        False,
    ),
    (
        "pinpoint",
        "https://{s}.pinpointhq.com/postings.json",
        lambda d: isinstance(d, (list, dict)) and bool(d),
        None,
        None,
        False,
    ),
]


def load() -> dict:
    """Read the registry, and forget past failures if the rules have changed.

    An employer that failed under the old slug rules is not evidence of
    anything under the new ones, but the 30 day retry window would keep it
    invisible for a month. Bumping RULES_VERSION clears the failures and
    keeps the confirmed employers, so a rule fix takes effect on the next
    run instead of next month.
    """
    if not REGISTRY.exists():
        return {"employers": [], "unresolved": {}, "rules_version": RULES_VERSION}
    reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    reg.setdefault("employers", [])
    reg.setdefault("unresolved", {})
    if reg.get("rules_version") != RULES_VERSION:
        dropped = len(reg["unresolved"])
        reg["unresolved"] = {}
        reg["rules_version"] = RULES_VERSION
        print(f"slug rules changed; retrying {dropped} previously unresolved employers")
    return reg


def probe_one(session: requests.Session, name: str, tier: str) -> dict | None:
    watchlist = str(tier) == "0"
    for slug, is_guess in slugs(name):
        for ats, tmpl, ok, name_tmpl, pick_name, verifiable in PATTERNS:
            # A single-word guess only goes to an ATS that will confirm the
            # company name, unless the employer is on the hand-checked
            # watchlist, where the name itself is the evidence.
            if is_guess and not verifiable and not watchlist:
                continue
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
                "guessed": is_guess,
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

    todo: list[tuple[str, str]] = []
    with SEED.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            n = row["name"]
            if n in resolved:
                continue
            last = reg["unresolved"].get(n)
            if last and last > cutoff:
                continue
            todo.append((n, row.get("tier", "")))

    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    todo = todo[:limit]
    print(f"probing {len(todo)} employers ({len(resolved)} already resolved)")

    session = requests.Session()
    found = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = pool.map(lambda p: probe_one(session, p[0], p[1]), todo)
        for (name, _tier), hit in zip(todo, results):
            if hit:
                reg["employers"].append(hit)
                reg["unresolved"].pop(name, None)
                found += 1
                bits = []
                if hit["confirmed_as"]:
                    bits.append("board says: " + hit["confirmed_as"])
                if hit["guessed"]:
                    bits.append("single-word guess")
                extra = f" ({'; '.join(bits)})" if bits else ""
                print(f"  + {name} -> {hit['ats']}/{hit['slug']}{extra}")
            else:
                reg["unresolved"][name] = date.today().isoformat()

    reg["employers"].sort(key=lambda e: e["name"].lower())
    reg["rules_version"] = RULES_VERSION
    REGISTRY.write_text(json.dumps(reg, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"resolved {found} new; registry now {len(reg['employers'])} employers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
