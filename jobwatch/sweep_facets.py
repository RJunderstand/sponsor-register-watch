#!/usr/bin/env python3
"""
sweep_facets.py — nightly jobs.ac.uk facet sweep with Gate 1 and Gate 2 applied.

Why this exists
---------------
The assistant used to scrape these facets by hand during its run. One facet cost
about 4k tokens and it could only afford four of the ten, so six went unswept
every day. This script sweeps all ten on a GitHub runner, joins each row against
the sponsor register, drops anything that fails the salary floor, and writes a
small JSON the assistant reads for almost nothing.

What it decides (mechanical only)
---------------------------------
Gate 1  employer appears on the register with Route == Skilled Worker
Gate 2  top of the advertised salary band >= NEW_ENTRANT_FLOOR

What it does NOT decide
-----------------------
Gate 3 (contract length) and Gate 4 (hard bars) need the JD read. Rows emitted
here are candidates, not decisions. `gate3_gate4 = "unassessed"` on every row.

Outputs
-------
jobwatch/facets_today.json   new candidates since the last run
jobwatch/facets_seen.json    every advert id ever emitted, so nothing repeats
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

REGISTER = REPO / "register.csv"
SEEN_PATH = HERE / "facets_seen.json"
OUT_PATH = HERE / "facets_today.json"

NEW_ENTRANT_FLOOR = 33_400

FACETS = [
    "pr-marketing-sales-and-communication",
    "international-activities",
    "student-services",
    "it-services",
    "web-design-and-development",
    "library-services-data-and-information-management",
    "administrative",
    "project-management-and-consulting",
    "legal-compliance-and-policy",
    "other",
]

# jobs.ac.uk ignores pageSize above 25 and silently returns 25. The first
# version of this script asked for 100 and then advanced startIndex by 100,
# which skipped 75 rows a page and meant only the first page of each facet was
# ever read. That was the exact limitation this script exists to remove.
# Advance by the number of rows actually parsed, never by a constant.
PAGE_SIZE = 25
MAX_PAGES_PER_FACET = 8  # 200 rows a facet; the facets are date-sorted

BASE = "https://www.jobs.ac.uk"
UA = "Mozilla/5.0 (compatible; sponsor-register-watch/1.0; +https://github.com/RJunderstand/sponsor-register-watch)"

# Keep in step with FAMILIES in fetch_jobs.py.
FAMILIES: dict[str, list[str]] = {
    "A_he_learning_tech": [
        "learning technolog", "learning design", "instructional design",
        "digital learning", "digital education", "educational technolog",
        "learning experience", "technology enhanced learning", "e-learning",
        "elearning", "curriculum design", "academic skills", "digital accessib",
        "inclusive design", "accessibility officer", "accessibility specialist",
    ],
    "B_corporate_ld": [
        "learning and development", "l&d", "training design", "capability",
        "upskilling", "learning partner", "learning consultant",
        "learning content", "training specialist", "learning specialist",
    ],
    "C_marketing_recruitment": [
        "student recruitment", "widening participation", "outreach officer",
        "marketing officer", "marketing executive", "marketing coordinator",
        "communications officer", "communications executive", "content officer",
        "campaigns", "international officer", "international recruitment",
        "international relations", "international engagement", "global engagement",
        "china", "partnerships officer", "engagement officer", "admissions",
        "brand", "social media", "digital marketing", "applicant",
        "enquiries", "conversion", "prospective student", "alumni",
    ],
    "D_edtech_customer": [
        "customer success", "implementation consultant", "onboarding",
        "customer education", "client services", "solutions consultant",
        "account manager", "customer experience", "partner success",
        "training consultant", "enablement",
    ],
    "E_learning_ops": [
        "learning operations", "l&d coordinator", "learning coordinator",
        "lms administrator", "training coordinator", "academy operations",
        "learning administrator", "programme coordinator",
    ],
    "F_quality_compliance": [
        "quality officer", "quality specialist", "quality assurance",
        "academic quality", "compliance officer", "policy officer",
        "standards officer", "governance officer", "apprenticeship quality",
    ],
    "G_content_multimedia": [
        "content producer", "content designer", "multimedia", "video producer",
        "motion design", "graphic design", "creative producer", "digital content",
        "content creator", "content manager", "editorial",
    ],
    "H_ai_governance": [
        "ai governance", "responsible ai", "ai policy", "ai assurance",
        "ai enablement", "ai literacy", "ai adoption", "data ethics",
        "ai compliance", "ai risk",
    ],
    "I_document_accessibility": ["accessib", "wcag", "inclusive content", "document remediation"],
    "J_process_ops": [
        "process improvement", "business analyst", "service improvement",
        "operations analyst", "continuous improvement", "business change",
    ],
}

EXCLUDE = re.compile(
    r"\b(senior manager|head of|director|vice president|vp |chief |principal"
    r"|lecturer|professor|postdoc|phd|research fellow|research associate"
    r"|software engineer|developer|devops|sre|data scientist|data engineer"
    r"|nurse|nursing|care assistant|driver|chef|cleaner|security officer"
    r"|technician|porter|cleaner|catering|chaplain)\b",
    re.I,
)

RESULT_RE = re.compile(
    r'<div class="j-search-result__result[^"]*"\s+data-advert-id="(?P<advert>\d+)".*?</div>\s*</div>\s*</div>',
    re.S,
)
LINK_RE = re.compile(r'<a href="(/job/[^"]+)"[^>]*>(.*?)</a>', re.S)
EMPLOYER_RE = re.compile(r'j-search-result__employer">\s*<b>(.*?)</b>', re.S)
DEPT_RE = re.compile(r'j-search-result__department">(.*?)</div>', re.S)
LOCATION_RE = re.compile(r'<div>\s*Location:\s*(.*?)\s*</div>', re.S)
SALARY_RE = re.compile(r'<strong>Salary:\s*</strong>\s*(.*?)\s*</div>', re.S)
CLOSES_RE = re.compile(r'date--blue[^>]*>\s*(.*?)\s*</span>', re.S)
FOUND_RE = re.compile(r'([\d,]+)\s+Jobs?\s+Found', re.I)


def clean(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).replace("\xa0", " ").strip()


def families_for(title: str) -> list[str]:
    t = title.lower()
    return [fam for fam, words in FAMILIES.items() if any(w in t for w in words)]


def parse_salary(raw: str) -> tuple[int | None, int | None, str]:
    """Return (bottom, top, note). Both None means the band could not be read."""
    if not raw:
        return None, None, "SALARY_UNKNOWN"
    txt = raw.replace(",", "")
    nums = [int(n) for n in re.findall(r"£\s*(\d{4,7})", txt)]
    if not nums:
        return None, None, "SALARY_UNKNOWN"
    bottom, top = min(nums), max(nums)
    note = ""
    if re.search(r"\bpro[- ]?rata\b|\bper hour\b|\bhourly\b|\bp/h\b", raw, re.I):
        note = "CHECK_HOURS"       # Gate 2 trap: the Derby 18.5-hour case
    if re.search(r"\bpart[- ]?time\b", raw, re.I):
        note = "CHECK_HOURS"
    return bottom, top, note


def load_register() -> dict[str, list[tuple[str, str, str]]]:
    """Map a normalised employer name to its register rows."""
    idx: dict[str, list[tuple[str, str, str]]] = {}
    if not REGISTER.exists():
        print(f"WARNING: {REGISTER} missing; Gate 1 will be skipped", file=sys.stderr)
        return idx
    with REGISTER.open(encoding="utf8", errors="ignore") as fh:
        next(fh, None)
        for line in fh:
            parts = [p.strip().strip('"') for p in line.split(",")]
            if len(parts) < 5:
                continue
            name, town, county, rating, route = parts[0], parts[1], parts[2], parts[-2], parts[-1]
            idx.setdefault(normalise(name), []).append((name, rating, route))
    return idx


NOISE = re.compile(
    r"\b(the|university|of|and|ltd|limited|llp|plc|group|uk|college|"
    r"institute|trust|services|holdings)\b",
    re.I,
)


def normalise(name: str) -> str:
    n = html.unescape(name or "").lower()
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = NOISE.sub(" ", n)
    return " ".join(n.split())


# Advert name -> register name, where the two differ. Extend as they turn up.
#
# The UWE line is the reason this table exists. "UWE, Bristol" normalises to
# "uwe bristol"; the register calls it "University of the West of England"
# ("west england"), and it separately lists "UWE Houses LLP" ("uwe houses"),
# which is a student accommodation company and NOT the university. Without an
# explicit alias the advert either matches nothing or matches the wrong entity.
ALIASES = {
    "uwe bristol": "west england",
    "uwe": "west england",
    "greater manchester": "bolton",
    "uclan": "lancashire",
    "central lancashire": "lancashire",
    "lase": "greenwich",
    "london south east": "greenwich",
    "city st georges london": "city st georges",
    "imperial london": "imperial london hr",
    "arts london": "arts london",
}

# Register entries that must never be matched by a fuzzy fallback, because a
# similarly named entity exists that is not the employer advertising.
NEVER_FUZZY = {"uwe houses", "imperial dental practice", "aru recruitment"}


def match_register(employer: str, reg: dict) -> dict | None:
    if not reg:
        return None
    key = normalise(employer)
    key = ALIASES.get(key, key)
    rows = reg.get(key)
    if not rows:
        # Containment fallback, longest register key first. Requires a real
        # overlap, not an incidental one, and skips the known decoys.
        for cand in sorted(reg, key=len, reverse=True):
            if not cand or len(cand) <= 6 or cand in NEVER_FUZZY:
                continue
            if cand in key or key in cand:
                rows = reg[cand]
                break
    if not rows:
        return None
    skilled = [r for r in rows if "skilled worker" in r[2].lower()]
    if not skilled:
        return {"matched_name": rows[0][0], "rating": rows[0][1],
                "route": rows[0][2], "skilled_worker": False}
    return {"matched_name": skilled[0][0], "rating": skilled[0][1],
            "route": skilled[0][2], "skilled_worker": True}


def fetch(url: str, session: requests.Session, tries: int = 3) -> str:
    for attempt in range(tries):
        try:
            r = session.get(url, timeout=40)
            if r.status_code == 200:
                return r.text
            print(f"  {r.status_code} on {url}", file=sys.stderr)
        except requests.RequestException as exc:
            print(f"  {exc} on {url}", file=sys.stderr)
        time.sleep(3 * (attempt + 1))
    return ""


def sweep_facet(slug: str, session: requests.Session) -> list[dict]:
    rows, start, total = [], 0, None
    for page in range(MAX_PAGES_PER_FACET):
        url = (f"{BASE}/search/{slug}?activeFacet=nonAcademicDisciplineFacet"
               f"&sortOrder=1&pageSize={PAGE_SIZE}&startIndex={start}"
               f"&nonAcademicDisciplineFacet%5B0%5D={slug}")
        body = fetch(url, session)
        if not body:
            break
        if total is None:
            m = FOUND_RE.search(body)
            # Must stay None when the count cannot be read. An earlier version
            # used 0 here, which made "start >= total" true immediately and
            # stopped every facet after its first page. That is the whole bug
            # this script was written to avoid.
            total = int(m.group(1).replace(",", "")) if m else None
            print(f"{slug}: {total if total is not None else 'count unknown'} jobs found")
        found_on_page = 0
        for block in RESULT_RE.finditer(body):
            chunk = block.group(0)
            link = LINK_RE.search(chunk)
            if not link:
                continue
            found_on_page += 1
            title = clean(link.group(2))
            rows.append({
                "advert_id": block.group("advert"),
                "title": title,
                "employer": clean((EMPLOYER_RE.search(chunk) or [None, ""])[1]
                                  if EMPLOYER_RE.search(chunk) else ""),
                "department": clean((DEPT_RE.search(chunk) or [None, ""])[1]
                                    if DEPT_RE.search(chunk) else ""),
                "location": clean((LOCATION_RE.search(chunk) or [None, ""])[1]
                                  if LOCATION_RE.search(chunk) else ""),
                "salary_raw": clean((SALARY_RE.search(chunk) or [None, ""])[1]
                                    if SALARY_RE.search(chunk) else ""),
                "closes": clean((CLOSES_RE.search(chunk) or [None, ""])[1]
                                if CLOSES_RE.search(chunk) else ""),
                "url": BASE + link.group(1),
                "facet": slug,
            })
        if found_on_page == 0:
            break
        start += found_on_page          # never a constant; see the PAGE_SIZE note
        if total is not None and start >= total:
            break
        time.sleep(2)
    print(f"  {slug}: collected {len(rows)} of {total}")
    return rows


def main() -> int:
    seen = set()
    if SEEN_PATH.exists():
        try:
            seen = set(json.loads(SEEN_PATH.read_text(encoding="utf8")))
        except json.JSONDecodeError:
            pass

    reg = load_register()
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})

    raw: list[dict] = []
    for slug in FACETS:
        try:
            raw.extend(sweep_facet(slug, session))
        except Exception as exc:                       # one bad facet must not kill the run
            print(f"{slug}: FAILED {exc}", file=sys.stderr)

    # De-duplicate within this run: the same advert appears under several facets.
    by_id: dict[str, dict] = {}
    for row in raw:
        by_id.setdefault(row["advert_id"], row)

    kept, stats = [], {"seen_before": 0, "excluded_title": 0, "gate1": 0, "gate2": 0}
    for advert_id, row in by_id.items():
        if advert_id in seen:
            stats["seen_before"] += 1
            continue
        if EXCLUDE.search(row["title"]):
            stats["excluded_title"] += 1
            continue

        bottom, top, note = parse_salary(row["salary_raw"])
        if top is not None and top < NEW_ENTRANT_FLOOR:
            stats["gate2"] += 1
            continue

        match = match_register(row["employer"], reg)
        if reg and (match is None or not match["skilled_worker"]):
            stats["gate1"] += 1
            continue

        row.update({
            "families": families_for(row["title"]),
            "salary_bottom": bottom,
            "salary_top": top,
            "salary_note": note or ("SALARY_UNKNOWN" if top is None else ""),
            "gate2": ("达标" if bottom is not None and bottom >= NEW_ENTRANT_FLOOR
                      else "需谈薪至门槛以上" if top is not None
                      else "未知"),
            "register_match": match,
            "gate3_gate4": "unassessed",
        })
        kept.append(row)
        seen.add(advert_id)

    # Most useful first: a family hit, then the earliest closing date.
    kept.sort(key=lambda r: (not r["families"], r.get("closes") or "zz"))

    OUT_PATH.write_text(json.dumps({
        "generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "run_date": date.today().isoformat(),
        "floor": NEW_ENTRANT_FLOOR,
        "facets_swept": FACETS,
        "counts": {"raw": len(raw), "unique": len(by_id), "kept": len(kept), **stats},
        "note": "Gate 1 and Gate 2 applied mechanically. Gate 3 and Gate 4 need the JD.",
        "jobs": kept,
    }, ensure_ascii=False, indent=1), encoding="utf8")

    SEEN_PATH.write_text(json.dumps(sorted(seen), indent=0), encoding="utf8")

    print(f"swept {len(raw)} rows, {len(by_id)} unique, kept {len(kept)}")
    print(f"dropped: seen {stats['seen_before']}, title {stats['excluded_title']}, "
          f"gate1 {stats['gate1']}, gate2 {stats['gate2']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
