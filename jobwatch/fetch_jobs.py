#!/usr/bin/env python3
"""
Step 3 of the job-watch pipeline. Runs daily.

Pulls every endpoint in ats_registry.json, normalises the postings, keeps the
UK ones whose title matches RJ's target families, diffs against yesterday and
writes the result.

Outputs:
  all_current.json  every matching live posting (the state file for diffing)
  new_today.json    only postings that were not there yesterday  <- Claude reads this

new_today.json is deliberately tiny: title, employer, location, date, url and
which family matched. No descriptions. A hundred rows costs about 2k tokens,
which means the assistant can read every single line instead of sampling.
"""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import requests

REGISTRY = Path("ats_registry.json")
STATE = Path("all_current.json")
NEW = Path("new_today.json")
TIMEOUT = 12
WORKERS = 12
UA = {"User-Agent": "rj-jobwatch/1.0 (personal job search; contact via repo)"}

# Families A to J from SKILL.md. Deliberately broad: this filter must not
# recreate the skim-reading problem. Anything plausible gets through and the
# assistant makes the real judgement after reading the JD.
FAMILIES: dict[str, list[str]] = {
    "A_he_learning_tech": [
        "learning technolog", "learning design", "instructional design",
        "digital learning", "digital education", "educational technolog",
        "learning experience", "technology enhanced learning", "tel officer",
        "e-learning", "elearning", "curriculum design", "academic skills",
        "digital accessib", "inclusive design", "accessibility officer",
        "accessibility specialist", "accessibility consultant",
    ],
    "B_corporate_ld": [
        "learning and development", "l&d", "training design", "training manager",
        "capability", "upskilling", "learning partner", "learning consultant",
        "learning content", "training specialist", "learning specialist",
    ],
    "C_marketing_recruitment": [
        "student recruitment", "widening participation", "outreach officer",
        "marketing officer", "marketing executive", "marketing coordinator",
        "communications officer", "communications executive", "content officer",
        "campaigns", "international officer", "international recruitment",
        "china", "partnerships officer", "engagement officer", "admissions",
        "brand", "social media", "digital marketing",
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
    "I_document_accessibility": [
        "accessib", "wcag", "inclusive content", "document remediation",
    ],
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
    r"|sales executive|business development manager|recruitment consultant)\b",
    re.I,
)

UK = re.compile(
    r"\b(united kingdom|uk|england|scotland|wales|northern ireland|london"
    r"|manchester|birmingham|leeds|liverpool|bristol|glasgow|edinburgh"
    r"|cardiff|belfast|sheffield|newcastle|nottingham|cambridge|oxford"
    r"|brighton|reading|coventry|salford|remote)\b",
    re.I,
)


def families_for(title: str) -> list[str]:
    t = title.lower()
    return [fam for fam, words in FAMILIES.items() if any(w in t for w in words)]


def norm(ats: str, employer: str, raw: dict) -> dict | None:
    """Flatten one posting from whichever ATS it came from."""
    try:
        if ats == "greenhouse":
            title = raw.get("title", "")
            loc = (raw.get("location") or {}).get("name", "")
            url = raw.get("absolute_url", "")
            posted = (raw.get("updated_at") or "")[:10]
        elif ats == "lever":
            title = raw.get("text", "")
            loc = (raw.get("categories") or {}).get("location", "")
            url = raw.get("hostedUrl", "")
            posted = ""
        elif ats == "ashby":
            title = raw.get("title", "")
            loc = raw.get("location", "")
            url = raw.get("jobUrl", "")
            posted = (raw.get("publishedAt") or "")[:10]
        elif ats == "smartrecruiters":
            title = raw.get("name", "")
            loc = " ".join(
                str(v) for v in (raw.get("location") or {}).values() if isinstance(v, str)
            )
            url = f"https://jobs.smartrecruiters.com/{raw.get('id','')}"
            posted = (raw.get("releasedDate") or "")[:10]
        elif ats == "recruitee":
            title = raw.get("title", "")
            loc = raw.get("location", "")
            url = raw.get("careers_url", "")
            posted = (raw.get("published_at") or "")[:10]
        else:  # teamtailor, pinpoint and anything else with loose shapes
            title = raw.get("title") or raw.get("name") or ""
            loc = str(raw.get("location") or raw.get("city") or "")
            url = raw.get("url") or raw.get("careers_url") or ""
            posted = str(raw.get("created_at") or raw.get("published_at") or "")[:10]
    except Exception:
        return None

    if not title:
        return None
    return {
        "employer": employer,
        "title": title.strip(),
        "location": (loc or "").strip(),
        "url": url,
        "posted": posted,
        "ats": ats,
    }


def postings(ats: str, data) -> list[dict]:
    if ats in ("greenhouse", "ashby"):
        return data.get("jobs", []) if isinstance(data, dict) else []
    if ats == "lever":
        return data if isinstance(data, list) else []
    if ats == "smartrecruiters":
        return data.get("content", []) if isinstance(data, dict) else []
    if ats == "recruitee":
        return data.get("offers", []) if isinstance(data, dict) else []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "jobs", "postings", "results"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def pull(session: requests.Session, emp: dict) -> list[dict]:
    try:
        r = session.get(emp["endpoint"], timeout=TIMEOUT, headers=UA)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []

    out = []
    for raw in postings(emp["ats"], data):
        if not isinstance(raw, dict):
            continue
        rec = norm(emp["ats"], emp["name"], raw)
        if not rec:
            continue
        if EXCLUDE.search(rec["title"]):
            continue
        fams = families_for(rec["title"])
        if not fams:
            continue
        if rec["location"] and not UK.search(rec["location"]):
            continue
        rec["families"] = fams
        out.append(rec)
    return out


def key(rec: dict) -> str:
    return f"{rec['employer']}|{rec['title']}|{rec['location']}"


def main() -> int:
    if not REGISTRY.exists():
        print("ats_registry.json missing; run probe_ats.py first", file=sys.stderr)
        return 1

    reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    employers = reg.get("employers", [])
    print(f"pulling {len(employers)} feeds")

    session = requests.Session()
    current: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for chunk in pool.map(lambda e: pull(session, e), employers):
            current.extend(chunk)

    previous = set()
    if STATE.exists():
        try:
            previous = {key(r) for r in json.loads(STATE.read_text(encoding="utf-8"))}
        except Exception:
            previous = set()

    fresh = [r for r in current if key(r) not in previous]
    fresh.sort(key=lambda r: (r["families"][0], r["employer"].lower()))

    STATE.write_text(json.dumps(current, indent=1, ensure_ascii=False), encoding="utf-8")
    NEW.write_text(
        json.dumps(
            {"generated": date.today().isoformat(), "count": len(fresh), "jobs": fresh},
            indent=1,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    by_fam: dict[str, int] = {}
    for r in fresh:
        for f in r["families"]:
            by_fam[f] = by_fam.get(f, 0) + 1
    print(f"{len(current)} matching live postings, {len(fresh)} new today")
    for f in sorted(by_fam):
        print(f"  {f}: {by_fam[f]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
