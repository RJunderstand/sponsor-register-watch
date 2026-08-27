#!/usr/bin/env python3
"""
Step 1 of the job-watch pipeline.

Reads the sponsor register that this repo already mirrors and produces
seed_employers.csv: every A-rated Skilled Worker licence holder whose name or
town suggests it is worth watching for RJ.

No network access. Runs in about a second on 142k rows.

Output columns: name, town, county, tier
  tier 1 = higher education institution
  tier 2 = education / EdTech / training / publishing
  tier 3 = technology / data / fintech / digital
"""

import csv
import re
import sys
from pathlib import Path

REGISTER = Path(sys.argv[1] if len(sys.argv) > 1 else "register.csv")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "seed_employers.csv")

# Tier 1: higher and further education providers.
HE = re.compile(
    r"\b(universit|college|institute of|school of|conservatoi|academy of"
    r"|polytechnic|business school|medical school)\b",
    re.I,
)

# Tier 2: education, training, EdTech, academic publishing.
EDU = re.compile(
    r"\b(education|edtech|e-?learning|learning|training|tuition|tutor|teach"
    r"|curriculum|pedagog|academy|schools?|awarding|examination|assessment"
    r"|publish|press|scholar|student|apprentic|skills)\b",
    re.I,
)

# Tier 3: technology, data, digital, fintech.
TECH = re.compile(
    r"\b(technolog|software|digital|data|cloud|cyber|fintech|payments"
    r"|analytics|artificial intelligence|machine learning|platform|systems"
    r"|informatics|computing|innovation|labs?)\b",
    re.I,
)

# Obvious false positives: care homes, takeaways, driving schools, etc.
# These words next to an education word almost always mean it is not a target.
NOISE = re.compile(
    r"\b(care home|nursing|domicil|takeaway|restaurant|kebab|barber|salon"
    r"|driving school|dental|pharmac|construction|scaffold|plumb|roofing"
    r"|logistics|haulage|courier|cleaning|security services|recruitment agenc"
    r"|car wash|mot |tyre|halal|grocer|supermarket|newsagent)\b",
    re.I,
)

# Locations she can realistically take. Remote-friendly employers are usually
# headquartered in one of these anyway, and tier 1 is kept regardless of town.
NEAR = re.compile(
    r"\b(london|manchester|salford|stockport|bolton|warrington|trafford"
    r"|greater manchester|leeds|liverpool|birmingham|bristol|reading"
    r"|cambridge|oxford|brighton|milton keynes|coventry|nottingham"
    r"|sheffield|newcastle|glasgow|edinburgh|cardiff|remote)\b",
    re.I,
)


def main() -> int:
    if not REGISTER.exists():
        print(f"register not found: {REGISTER}", file=sys.stderr)
        return 1

    seen: set[str] = set()
    rows: list[tuple[str, str, str, int]] = []

    with REGISTER.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        for rec in reader:
            if len(rec) < 5:
                continue
            name, town, county, rating, route = (c.strip() for c in rec[:5])

            if "skilled worker" not in route.lower():
                continue
            if "a rating" not in rating.lower():
                continue
            if NOISE.search(name):
                continue

            if HE.search(name):
                tier = 1
            elif EDU.search(name):
                tier = 2
            elif TECH.search(name):
                tier = 3
            else:
                continue

            # Tier 1 is kept wherever it is. Tiers 2 and 3 must be somewhere
            # she could actually work.
            if tier != 1 and not NEAR.search(f"{town} {county}"):
                continue

            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            rows.append((name, town, county, tier))

    rows.sort(key=lambda r: (r[3], r[0].lower()))

    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "town", "county", "tier"])
        w.writerows(rows)

    by_tier: dict[int, int] = {}
    for _, _, _, t in rows:
        by_tier[t] = by_tier.get(t, 0) + 1
    print(f"wrote {len(rows)} employers to {OUT}")
    for t in sorted(by_tier):
        print(f"  tier {t}: {by_tier[t]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
