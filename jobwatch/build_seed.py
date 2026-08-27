#!/usr/bin/env python3
"""
Step 1 of the job-watch pipeline.

Reads the sponsor register this repo mirrors and produces seed_employers.csv:
A-rated Skilled Worker licence holders worth probing for a job feed.

No network access. Runs in about a second on 142k rows.

v2, 2026-08-27. Two bugs fixed after the first run produced a seed containing
no universities and none of the named target employers:

  1. Every stem keyword was written as r"\buniversit\b", which can never match
     "University" because the trailing \b demands a boundary straight after
     "universit". The same mistake silenced technolog, publish, apprentic and
     pedagog. Stems now have no trailing boundary.

  2. Name keywords cannot find Coursera, Jisc, Kortext, AbilityNet, Unibuddy
     or Multiverse, because those names contain no sector word at all. A
     curated WATCHLIST is now seeded as tier 0 and always included.

Tier order is also the probe order, so the employers that matter are resolved
first rather than alphabetically last:
  tier 0 = named watchlist
  tier 3 = technology, data, digital, fintech   (highest ATS adoption)
  tier 2 = education, training, EdTech, publishing
  tier 1 = universities and colleges            (lowest ATS adoption)
"""

import csv
import re
import sys
from pathlib import Path

REGISTER = Path(sys.argv[1] if len(sys.argv) > 1 else "register.csv")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "seed_employers.csv")

# Tier 0. These are the employers already confirmed as licensed and relevant,
# plus the ones whose names carry no sector keyword and would otherwise be
# invisible to any keyword filter.
WATCHLIST = [
    # EdTech and learning platforms
    "coursera", "d2l", "instructure", "turnitin", "kortext", "studiosity",
    "multiverse", "futurelearn", "blackboard", "unibuddy", "anthology",
    "docebo", "moodle", "learnosity", "century tech", "sparx", "firefly learning",
    "bibliu", "perlego", "pearson", "cambridge university press",
    "oxford university press", "sage publications", "taylor & francis",
    # International recruitment and pathway
    "study group", "kaplan", "idp connect", "oxford international",
    "quacquarelli", "bpp", "arden university", "navitas", "into university",
    "shorelight", "keystone education",
    # Sector bodies, accessibility, quality
    "jisc", "advance he", "abilitynet", "zoonou", "digital accessibility centre",
    "quality assurance agency", "city & guilds", "ncfe", "aqa", "the open university",
    # Employers already confirmed relevant in the tracker
    "ucl", "university college london", "manchester metropolitan",
    "birmingham city university", "imperial college", "kingfisher plc",
    "john crane", "hastings insurance", "ntt data", "wellcome trust",
    "abb limited", "suez", "gowling", "elemis", "l'occitane", "costa",
    "save the children", "jpmorgan", "bank of new york mellon", "bt group",
]

# Stems deliberately have no trailing \b. "universit" must match "University",
# "universities" and "university's".
HE = re.compile(
    r"\b(universit|college|institute|school of|conservatoi|academy of"
    r"|polytechnic|business school|medical school|further education"
    r"|sixth form)",
    re.I,
)

EDU = re.compile(
    r"\b(education|edtech|e-?learning|learning|training|tuition|tutor"
    r"|teach|curriculum|pedagog|academy|awarding|examination|assessment"
    r"|publish|press|scholar|student|apprentic|skills|literacy|coaching)",
    re.I,
)

TECH = re.compile(
    r"\b(technolog|software|digital|data|cloud|cyber|fintech|payments"
    r"|analytic|artificial intelligence|machine learning|platform|systems"
    r"|informatic|computing|innovation|labs|studio|interactive|media)",
    re.I,
)

# Obvious false positives.
NOISE = re.compile(
    r"\b(care home|nursing home|domicil|takeaway|restaurant|kebab|barber|salon"
    r"|driving school|dental|pharmac|construction|scaffold|plumb|roofing"
    r"|haulage|courier|cleaning|car wash|tyre|halal|grocer|supermarket"
    r"|newsagent|nursery|pre-?school|primary school|day care|childcare"
    r"|beauty|massage|fitness|takeaways|catering)",
    re.I,
)

# Tiers 2 and 3 must be somewhere she could work. Tiers 0 and 1 are kept
# wherever they are.
NEAR = re.compile(
    r"\b(london|manchester|salford|stockport|bolton|warrington|trafford"
    r"|greater manchester|leeds|liverpool|birmingham|bristol|reading"
    r"|cambridge|oxford|brighton|milton keynes|coventry|nottingham"
    r"|sheffield|newcastle|glasgow|edinburgh|cardiff|remote|bournemouth"
    r"|southampton|york|hertfordshire|surrey|berkshire|greater london)",
    re.I,
)


# Word boundaries, not substrings. "ucl" must not match "nuclear" and "aqa"
# must not match "Baraqat"; both slipped into tier 0 on the first attempt.
WATCH_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in WATCHLIST) + r")\b", re.I
)


def tier_of(name: str, town: str, county: str) -> int | None:
    if WATCH_RE.search(name):
        return 0
    if NOISE.search(name):
        return None
    if TECH.search(name):
        return 3 if NEAR.search(f"{town} {county}") else None
    if EDU.search(name):
        return 2 if NEAR.search(f"{town} {county}") else None
    if HE.search(name):
        return 1
    return None


def main() -> int:
    if not REGISTER.exists():
        print(f"register not found: {REGISTER}", file=sys.stderr)
        return 1

    seen: set[str] = set()
    rows: list[tuple[str, str, str, int]] = []

    with REGISTER.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        for rec in reader:
            if len(rec) < 5:
                continue
            name, town, county, rating, route = (c.strip() for c in rec[:5])
            if "skilled worker" not in route.lower():
                continue
            if "a rating" not in rating.lower():
                continue
            tier = tier_of(name, town, county)
            if tier is None:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            rows.append((name, town, county, tier))

    # Probe order: watchlist, then tech, then education, then universities.
    order = {0: 0, 3: 1, 2: 2, 1: 3}
    rows.sort(key=lambda r: (order[r[3]], r[0].lower()))

    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "town", "county", "tier"])
        w.writerows(rows)

    counts: dict[int, int] = {}
    for _, _, _, t in rows:
        counts[t] = counts.get(t, 0) + 1
    print(f"wrote {len(rows)} employers to {OUT}")
    for t in sorted(counts):
        label = {0: "watchlist", 1: "universities and colleges",
                 2: "education and EdTech", 3: "technology and data"}[t]
        print(f"  tier {t} ({label}): {counts[t]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
