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
from html import unescape as html_unescape

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
    
        "alumni",
        "development officer",
        "philanthropy",
        "supporter relations",
        "donor",
        "fundraising",
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
    
        "intranet",
        "content strategy",
        "content and experience",
    ],
    "H_ai_governance": [
        "ai governance", "responsible ai", "ai policy", "ai assurance",
        "ai enablement", "ai literacy", "ai adoption", "data ethics",
        "ai compliance", "ai risk",
    ],
    "I_document_accessibility": [
        "accessib", "wcag", "inclusive content", "document remediation",
    ],
    "K_student_support": [
        "wellbeing", "well-being", "welfare", "student support",
        "student experience", "disability", "safeguarding", "pastoral",
        "mental health", "inclusion officer", "learning support",
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

# A named foreign country beats a bare "Remote". The first run let through
# "Remote - Philippines" and "United States - Remote" because both contain
# the word remote.
NOT_UK = re.compile(
    r"\b(philippines|united states|usa|canada|india|australia|germany"
    r"|france|spain|portugal|poland|romania|netherlands|belgium|sweden|norway"
    r"|denmark|switzerland|austria|italy|greece|turkey|israel|uae|dubai"
    r"|singapore|malaysia|japan|korea|hong kong|china|taiwan|brazil|mexico"
    r"|argentina|colombia|chile|south africa|nigeria|kenya|egypt|new zealand"
    r"|dublin)\b",
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
            loc_raw = raw.get("location") or raw.get("city") or ""
            if isinstance(loc_raw, dict):
                # Pinpoint returns {"id":..,"city":..,"region":..,"country":..}
                loc = " ".join(
                    str(loc_raw.get(k))
                    for k in ("city", "region", "state", "country", "name")
                    if isinstance(loc_raw.get(k), str)
                )
            elif isinstance(loc_raw, list):
                loc = " ".join(str(x) for x in loc_raw if isinstance(x, str))
            else:
                loc = str(loc_raw)
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


# ---------------------------------------------------------------------------
# HTML career pages (no JSON API). Added 2026-09-06 for the pathway providers
# RJ asked for: Study Group (JazzHR) and Kaplan International (HireHive).
# Both pages are server-rendered, so a regex over the listing page is enough.
# ---------------------------------------------------------------------------
# JazzHR: <li class="list-group-item"> <h3><a href="https://x.applytojob.com/apply/ID/Slug">Title</a></h3>
#         <ul class="list-inline ..."><li><i class="fa fa-map-marker"></i>City, Region, Country</li> ...</ul></li>
JAZZHR_ITEM = re.compile(r'<li class="list-group-item">(?P<body>.*?)</ul>\s*</li>', re.S)
JAZZHR_LINK = re.compile(r'<a[^>]+href="(?P<url>https?://[^"]+/apply/[^"]+)"[^>]*>(?P<title>.*?)</a>', re.S)
JAZZHR_LOC = re.compile(r'fa-map-marker[^>]*>(?:\s*</i>)?\s*(?P<loc>[^<]+?)\s*</li>', re.S)
JAZZHR_LI = re.compile(r'<li[^>]*>(?P<li>.*?)</li>', re.S)
# HireHive: <a href="/slug-ID" class="... hh-job-row ..."> <h3 ...hh-job-row-title><span>Title</span></h3>
#           <div ...hh-job-row-location> <svg/> City, Country </div> <div ...hh-job-row-experience> <svg/> Full Time </div> </a>
HIREHIVE_ROW = re.compile(r'<a[^>]+href="(?P<href>/[^"]+)"[^>]*hh-job-row[^>]*>(?P<body>.*?)</a>', re.S)
HIREHIVE_TITLE = re.compile(r'hh-job-row-title[^>]*>(?P<t>.*?)</h3>', re.S)
HIREHIVE_LOC = re.compile(r'hh-job-row-location[^>]*>(?P<l>.*?)</div>', re.S)
HIREHIVE_TYPE = re.compile(r'hh-job-row-experience[^>]*>(?P<x>.*?)</div>', re.S)
TAG = re.compile(r'<[^>]+>')
SVG = re.compile(r'<svg.*?</svg>', re.S)


def _text(fragment: str) -> str:
    return re.sub(r'\s+', ' ', html_unescape(TAG.sub(' ', SVG.sub(' ', fragment or '')))).strip()


def pull_html(session: requests.Session, emp: dict) -> list[dict]:
    try:
        r = session.get(emp["endpoint"], timeout=TIMEOUT, headers=UA)
        if r.status_code != 200:
            return []
        page = r.text
    except Exception:
        return []
    out, seen = [], set()
    if emp["ats"] == "jazzhr":
        for item in JAZZHR_ITEM.finditer(page):
            body = item["body"]
            link = JAZZHR_LINK.search(body)
            if not link or link["url"] in seen:
                continue
            seen.add(link["url"])
            locm = JAZZHR_LOC.search(body)
            loc = _text(locm["loc"]) if locm else ""
            if not loc:  # fall back: first <li> whose text looks like "City, Country"
                for li in JAZZHR_LI.finditer(body):
                    t = _text(li["li"])
                    if "," in t:
                        loc = t
                        break
            out.append({"employer": emp["name"], "title": _text(link["title"]),
                        "location": loc, "url": link["url"],
                        "posted": "", "ats": "jazzhr"})
    elif emp["ats"] == "hirehive":
        base = emp["endpoint"].rstrip("/")
        for row in HIREHIVE_ROW.finditer(page):
            url = base + row["href"]
            if url in seen:
                continue
            seen.add(url)
            body = row["body"]
            t = HIREHIVE_TITLE.search(body); l = HIREHIVE_LOC.search(body); x = HIREHIVE_TYPE.search(body)
            title = _text(t["t"]) if t else ""
            if not title:
                continue
            loc = _text(l["l"]) if l else ""
            kind = _text(x["x"]) if x else ""
            out.append({"employer": emp["name"], "title": title + (f" ({kind})" if kind and kind != "Full Time" else ""),
                        "location": loc, "url": url, "posted": "", "ats": "hirehive"})
    return out


def pull(session: requests.Session, emp: dict) -> list[dict]:
    if emp["ats"] in ("jazzhr", "hirehive"):
        recs = pull_html(session, emp)
    else:
        try:
            r = session.get(emp["endpoint"], timeout=TIMEOUT, headers=UA)
            if r.status_code != 200:
                return []
            data = r.json()
        except Exception:
            return []
        recs = [norm(emp["ats"], emp["name"], raw)
                for raw in postings(emp["ats"], data) if isinstance(raw, dict)]

    out = []
    for rec in recs:
        if not rec:
            continue
        if EXCLUDE.search(rec["title"]):
            continue
        fams = families_for(rec["title"])
        if not fams:
            continue
        if rec["location"]:
            if NOT_UK.search(rec["location"]):
                continue
            if not UK.search(rec["location"]):
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
    # Small hand-maintained additions (pathway providers etc.) live in a separate
    # file so the 280 KB registry never has to be re-uploaded by hand.
    extra = REGISTRY.with_name("ats_registry_extra.json")
    if extra.exists():
        known = {e.get("name") for e in employers}
        for e in json.loads(extra.read_text(encoding="utf-8")).get("employers", []):
            if e.get("name") not in known:
                employers.append(e)
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
