#!/usr/bin/env python3
"""
prefilter.py  (2026-09-06, rule R15)

Zero-cost triage that runs BEFORE the assistant opens any link. Input is the
two feed files the GitHub Actions already produce; output is one small JSON
with three buckets and counts. Nothing here needs the network.

  IN    read queue, sorted: salary gate 达标 first, then family match, then closes
  FLAG  read, but check one named thing first (SOC / grade / hours)
  OUT   do not read, do not create a tracker row; one log line each

Rules (from RJ 2026-09-05/06):
  R15a  admin-family titles (Administrator / Administrative Assistant / Clerk /
        Receptionist / Secretary / Assistant unless senior) are SOC 4xxx, below
        RQF 6, so not sponsorable -> OUT unless the advert text says sponsor.
  R15b  salary_top < 33,400 -> OUT (already gate 2 in sweep; kept for ATS feed rows
        that carry a salary in the title).
  R15c  part-time / < 30 h / fixed term < 12 months in title -> OUT.
  R15d  Oxford/Cambridge grade <= 5 with a non-officer title -> FLAG.
  R15e  postings whose location names a non-UK country -> OUT.

Usage:
  python3 prefilter.py <repo>/jobwatch/facets_today.json <repo>/jobwatch/new_today.json prefilter_today.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FLOOR = 33_400

ADMIN_TITLE = re.compile(
    r"\b(administrator|administrative|admin assistant|admin officer|clerk|clerical|receptionist|secretary|"
    r"office assistant|team assistant|personal assistant|executive assistant|pa to|data entry|"
    r"assistant(?! (director|head|manager|registrar|dean|principal|professor|editor|producer|designer)))\b",
    re.I,
)
SPONSOR_OK = re.compile(r"\b(sponsor|sponsorship|certificate of sponsorship|skilled worker)\b", re.I)
PART_TIME = re.compile(r"\b(part[- ]time|0\.[0-8]\s*fte|(\d{1,2}(?:\.\d)?)\s*h(?:ou)?rs?\b|casual|sessional|hourly[- ]paid|bank\b|zero[- ]hours)", re.I)
SHORT_TERM = re.compile(r"\b(\d{1,2})\s*(months?|mths?)\b|\bmaternity cover\b|\bfixed[- ]term\b.*?\b([1-9]|1[01])\s*months?\b", re.I)
GRADE = re.compile(r"\bgrade\s*([1-9]|10)\b", re.I)
OFFICER_TITLE = re.compile(r"\b(officer|coordinator|co-ordinator|executive|specialist|adviser|advisor|manager|lead|analyst|designer|producer|developer|technologist|consultant)\b", re.I)
NOT_UK = re.compile(
    r"\b(philippines|united states|usa|canada|india|australia|germany|france|spain|portugal|poland|romania|"
    r"netherlands|belgium|sweden|norway|denmark|switzerland|austria|italy|greece|turkey|israel|uae|dubai|"
    r"united arab emirates|saudi|singapore|malaysia|japan|korea|hong kong|china|taiwan|brazil|mexico|"
    r"argentina|colombia|chile|south africa|nigeria|kenya|egypt|new zealand|dublin|ireland|vietnam|viet nam|"
    r"thailand|indonesia|kazakhstan)\b",
    re.I,
)


def money(s: str) -> list[int]:
    return [int(x.replace(",", "")) for x in re.findall(r"£\s?(\d{2,3},?\d{3})", s or "")]


def decide(row: dict) -> tuple[str, str]:
    title = row.get("title", "") or ""
    text = " ".join(str(row.get(k, "") or "") for k in ("title", "salary_raw", "location", "employer", "snippet", "summary"))
    loc = row.get("location", "") or ""

    if NOT_UK.search(loc) or NOT_UK.search(title):
        return "OUT", "R15e non-UK location"

    top = row.get("salary_top")
    if top is None:
        m = money(text)
        top = max(m) if m else None
    if top is not None and top < FLOOR:
        return "OUT", f"R15b salary top £{top:,} < £{FLOOR:,}"

    if PART_TIME.search(title) or PART_TIME.search(row.get("salary_raw", "") or ""):
        h = re.search(r"(\d{1,2}(?:\.\d)?)\s*h(?:ou)?rs?", text, re.I)
        if not h or float(h.group(1)) < 30:
            return "OUT", "R15c part-time / < 30 h / casual"

    st = SHORT_TERM.search(title)
    if st:
        months = st.group(1) or st.group(3)
        if months and int(months) < 12:
            return "OUT", f"R15c fixed term {months} months"
        if "maternity" in st.group(0).lower():
            return "FLAG", "R15c maternity cover: confirm >= 12 months"

    if ADMIN_TITLE.search(title) and not SPONSOR_OK.search(text):
        return "OUT", "R15a admin-family title (SOC 4xxx), advert does not mention sponsorship"

    g = GRADE.search(text)
    if g and int(g.group(1)) <= 5 and not OFFICER_TITLE.search(title):
        return "FLAG", f"R15d grade {g.group(1)} with non-officer title: check SOC before reading"

    return "IN", ""


def sort_key(row: dict) -> tuple:
    gate2 = row.get("gate2") or ""
    fam = row.get("families") or []
    return (0 if gate2 == "达标" else 1 if gate2 == "需谈薪至门槛以上" else 2,
            0 if fam else 1,
            row.get("closes") or "zz",
            row.get("employer", ""))


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(__doc__)
        return 1
    rows: list[dict] = []
    for src in argv[1:-1]:
        p = Path(src)
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        for r in d.get("jobs", []):
            r = dict(r)
            r["_source"] = p.name
            rows.append(r)

    buckets = {"IN": [], "FLAG": [], "OUT": []}
    for r in rows:
        b, why = decide(r)
        r["_why"] = why
        buckets[b].append(r)
    buckets["IN"].sort(key=sort_key)
    buckets["FLAG"].sort(key=sort_key)

    out = {
        "counts": {k: len(v) for k, v in buckets.items()} | {"total": len(rows)},
        "in": buckets["IN"],
        "flag": buckets["FLAG"],
        "out_log": [f"{r.get('employer','')} | {r.get('title','')} | {r['_why']}" for r in buckets["OUT"]],
    }
    Path(argv[-1]).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    c = out["counts"]
    print(f"prefilter: total {c['total']} -> IN {c['IN']} / FLAG {c['FLAG']} / OUT {c['OUT']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
