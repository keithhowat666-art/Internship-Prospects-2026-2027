"""
fetch_internships.py
--------------------
Pulls Canadian STEM internships from three free sources:

  1. Adzuna API      — broad Canadian job board aggregator
  2. Jobicy API      — remote-friendly tech roles, Canada filter, no key needed
  3. Canadian Space Agency — official CSA internship page (government HTML)

Deduplicates all results against internships.csv, appends new entries,
and rebuilds README.md automatically.

Usage:
    python scripts/fetch_internships.py

Environment variables required (for Adzuna only):
    ADZUNA_APP_ID    — https://developer.adzuna.com (free)
    ADZUNA_APP_KEY   — https://developer.adzuna.com (free)

Jobicy and CSA need no credentials whatsoever.
"""

import csv
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT     = Path(__file__).parent.parent
CSV_PATH = ROOT / "internships.csv"

MAX_AGE_DAYS = 60

CSV_FIELDNAMES = [
    "company", "role", "location", "term", "duration",
    "field", "link", "date_posted", "deadline", "notes",
]

# ── Field / term / duration inference ────────────────────────────────────────

FIELD_KEYWORDS = {
    "Software":            ["software", "web", "backend", "frontend", "full stack",
                            "devops", "cloud", "mobile", "ios", "android", "platform",
                            "developer", "programmer", "sre", "infrastructure"],
    "AI / Data":           ["machine learning", "data science", "data analyst", "deep learning",
                            "nlp", "computer vision", "artificial intelligence", "data engineer",
                            "analytics", "ml engineer", "llm", "generative ai"],
    "Engineering":         ["mechanical", "electrical", "mechatronics", "hardware", "embedded",
                            "controls", "manufacturing", "process engineer", "civil",
                            "structural", "chemical engineer"],
    "Aerospace":           ["aerospace", "avionics", "space", "satellite", "propulsion",
                            "flight", "uav", "drone", "astronautics", "rocketry"],
    "Research / Physics":  ["physics", "quantum", "photonics", "optics", "materials",
                            "research scientist", "lab assistant", "nanotechnology",
                            "biophysics", "theoretical", "computational science"],
    "Biotech":             ["biotech", "pharmaceutical", "pharma", "biology", "biochem",
                            "bioinformatics", "clinical", "life science", "medical device",
                            "genomics", "proteomics", "drug discovery"],
}

def infer_field(title: str, description: str, hint: str = "") -> str:
    text = (title + " " + description).lower()
    for field, keywords in FIELD_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return field
    return hint if hint else "Software"

def infer_term(title: str, description: str) -> str:
    text = (title + " " + description).lower()
    if any(kw in text for kw in ["winter", "january", "jan", "february", "feb", "w25", "w26"]):
        return "Winter"
    if any(kw in text for kw in ["fall", "september", "sept", "october", "autumn", "f25", "f26"]):
        return "Fall"
    return "Fall" if date.today().month <= 6 else "Winter"

def infer_duration(title: str, description: str) -> str:
    text = (title + " " + description).lower()
    if any(kw in text for kw in ["8 month", "8-month", "two term", "8mo", "double term"]):
        return "8"
    return "4"

def clean_location(raw: str) -> str:
    if not raw:
        return "Canada"
    raw = raw.strip()
    if raw.lower() in ("canada", "ca", ""):
        return "Canada"
    raw = re.sub(r"\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b", "", raw).strip().rstrip(",").strip()
    parts = [p.strip() for p in raw.split(",")]
    return ", ".join(parts[:2]) if len(parts) >= 2 else parts[0]

def is_intern_or_coop(title: str, description: str) -> bool:
    text = (title + " " + description).lower()
    return any(kw in text for kw in [
        "intern", "interne", "co-op", "coop", "co op",
        "student", "placement", "apprentice", "stage ",
    ])

def is_recent(dt_str: str) -> bool:
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return (datetime.now(dt.tzinfo) - dt).days <= MAX_AGE_DAYS
    except Exception:
        return True

def parse_date(dt_str: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return date.today().isoformat()

# ── HTTP helper ───────────────────────────────────────────────────────────────

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; internship-tracker/1.0)"}

def fetch_url(url: str, timeout: int = 15) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        print(f"  ⚠️  Fetch failed: {url[:80]}  ({e})")
        return None

# ── Source 1: Adzuna ──────────────────────────────────────────────────────────

ADZUNA_SEARCHES = [
    ("software intern",               "Software"),
    ("software developer co-op",      "Software"),
    ("machine learning intern",       "AI / Data"),
    ("data science intern",           "AI / Data"),
    ("data engineer co-op",           "AI / Data"),
    ("mechanical engineering co-op",  "Engineering"),
    ("electrical engineering intern", "Engineering"),
    ("mechatronics co-op",            "Engineering"),
    ("aerospace intern",              "Aerospace"),
    ("aerospace engineering co-op",   "Aerospace"),
    ("physics research intern",       "Research / Physics"),
    ("research assistant intern",     "Research / Physics"),
    ("biotech intern",                "Biotech"),
    ("pharmaceutical co-op",         "Biotech"),
]

def fetch_adzuna(app_id: str, app_key: str) -> list[dict]:
    print("\n📡  Source 1: Adzuna API")
    results = []

    for what, field_hint in ADZUNA_SEARCHES:
        print(f"  → {what!r}")
        params = urllib.parse.urlencode({
            "app_id": app_id, "app_key": app_key,
            "results_per_page": 10, "what": what,
            "content-type": "application/json", "sort_by": "date",
        })
        raw = fetch_url(f"https://api.adzuna.com/v1/api/jobs/ca/search/1?{params}")
        time.sleep(0.3)
        if not raw:
            continue

        try:
            jobs = json.loads(raw).get("results", [])
        except Exception:
            continue

        added = 0
        for job in jobs:
            title   = job.get("title", "").strip()
            desc    = job.get("description", "").strip()
            company = job.get("company", {}).get("display_name", "").strip()
            link    = job.get("redirect_url", "").strip()
            created = job.get("created", "")
            loc     = clean_location(job.get("location", {}).get("display_name", ""))

            if not is_intern_or_coop(title, desc): continue
            if not is_recent(created):             continue
            if not company or not title:           continue

            results.append({
                "company":     company,
                "role":        title,
                "location":    loc,
                "term":        infer_term(title, desc),
                "duration":    infer_duration(title, desc),
                "field":       infer_field(title, desc, field_hint),
                "link":        link,
                "date_posted": parse_date(created),
                "deadline":    "",
                "notes":       "",
                "_source":     "Adzuna",
            })
            added += 1
        print(f"     {added} result(s)")

    return results


# ── Source 2: Jobicy (free API, no key needed) ────────────────────────────────

JOBICY_SEARCHES = [
    ("intern",               "dev"),
    ("co-op",                "dev"),
    ("intern",               "data-science"),
    ("intern",               "engineering"),
]

def fetch_jobicy() -> list[dict]:
    print("\n📡  Source 2: Jobicy API (free, no key)")
    results = []

    for keyword, category in JOBICY_SEARCHES:
        params = urllib.parse.urlencode({
            "count": 20,
            "geo": "canada",
            "tag": keyword,
            "job_categories": category,
        })
        url = f"https://jobicy.com/api/v2/remote-jobs?{params}"
        print(f"  → {keyword!r} / {category}")
        raw = fetch_url(url)
        time.sleep(0.5)
        if not raw:
            continue

        try:
            jobs = json.loads(raw).get("jobs", [])
        except Exception:
            continue

        added = 0
        for job in jobs:
            title   = job.get("jobTitle", "").strip()
            desc    = job.get("jobDescription", "").strip()
            company = job.get("companyName", "").strip()
            link    = job.get("url", "").strip()
            created = job.get("pubDate", "")
            geo     = job.get("jobGeo", "Anywhere")

            if not is_intern_or_coop(title, desc): continue
            if not is_recent(created):             continue
            if not company or not title:           continue

            loc = "Remote / Canada" if geo in ("Anywhere", "") else clean_location(geo)

            results.append({
                "company":     company,
                "role":        title,
                "location":    loc,
                "term":        infer_term(title, desc),
                "duration":    infer_duration(title, desc),
                "field":       infer_field(title, desc),
                "link":        link,
                "date_posted": parse_date(created),
                "deadline":    "",
                "notes":       "Remote",
                "_source":     "Jobicy",
            })
            added += 1
        print(f"     {added} result(s)")

    return results


# ── Source 3: Canadian Space Agency internship page ───────────────────────────

CSA_URL = "https://www.asc-csa.gc.ca/eng/jobs/search-internships.asp"

def fetch_csa() -> list[dict]:
    print("\n📡  Source 3: Canadian Space Agency internship page")
    raw = fetch_url(CSA_URL)
    if not raw:
        return []

    text = raw.decode("utf-8", errors="ignore")
    results = []

    # CSA page lists internships in <li> or <td> elements with role + division + term
    # Pattern: "Title – Division – Term YYYY"
    pattern = re.compile(
        r'<a[^>]+href="([^"]+)"[^>]*>\s*([^<]{10,200}?)\s*</a>',
        re.IGNORECASE
    )

    seen_titles: set = set()
    for match in pattern.finditer(text):
        href, raw_title = match.group(1), match.group(2)
        title = html.unescape(raw_title).strip()

        # Skip navigation links
        if len(title) < 15 or any(nav in title.lower() for nav in [
            "back to", "home", "contact", "français", "english", "skip", "menu"
        ]):
            continue

        # Must look like a job title
        if not any(kw in title.lower() for kw in [
            "intern", "stage", "co-op", "student", "analyst", "engineer",
            "developer", "scientist", "technician", "research",
        ]):
            continue

        if title in seen_titles:
            continue
        seen_titles.add(title)

        # Build full URL if relative
        if href.startswith("/"):
            href = "https://www.asc-csa.gc.ca" + href
        elif not href.startswith("http"):
            href = CSA_URL

        # Infer term from title
        term = infer_term(title, "")

        results.append({
            "company":     "Canadian Space Agency",
            "role":        title,
            "location":    "Longueuil, QC",
            "term":        term,
            "duration":    infer_duration(title, ""),
            "field":       infer_field(title, "", "Aerospace"),
            "link":        href,
            "date_posted": date.today().isoformat(),
            "deadline":    "",
            "notes":       "Apply via co-op office",
            "_source":     "CSA",
        })

    print(f"     {len(results)} result(s)")
    return results


# ── CSV helpers ───────────────────────────────────────────────────────────────

def load_existing(path: Path) -> set:
    if not path.exists():
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {
            (r["company"].strip().lower(), r["role"].strip().lower())
            for r in csv.DictReader(f)
        }

def append_rows(path: Path, rows: list[dict]) -> None:
    write_header = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDNAMES})

def rebuild_readme() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "update_readme.py")],
        capture_output=True, text=True,
    )
    print(result.stdout.strip() if result.returncode == 0
          else f"⚠️  README update failed: {result.stderr.strip()}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app_id  = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")

    print("=" * 60)
    print("  Canadian STEM Internship Fetcher")
    print(f"  {date.today().isoformat()}")
    print("=" * 60)

    # ── Gather from all sources ───────────────────────────────────────────────
    all_found: list[dict] = []

    if app_id and app_key:
        all_found += fetch_adzuna(app_id, app_key)
    else:
        print("\n⚠️  ADZUNA_APP_ID / ADZUNA_APP_KEY not set — skipping Adzuna.")
        print("   Get a free key at: https://developer.adzuna.com")

    all_found += fetch_jobicy()
    all_found += fetch_csa()

    print(f"\n📋  Total found across all sources: {len(all_found)}")

    # ── Deduplicate ───────────────────────────────────────────────────────────
    existing    = load_existing(CSV_PATH)
    seen_in_run: set = set()
    new_rows: list[dict] = []

    for row in all_found:
        key = (row["company"].strip().lower(), row["role"].strip().lower())
        if key in existing or key in seen_in_run:
            continue
        new_rows.append(row)
        seen_in_run.add(key)

    if not new_rows:
        print("✅  No new internships found — CSV is already up to date.")
        return

    # ── Save + rebuild ────────────────────────────────────────────────────────
    append_rows(CSV_PATH, new_rows)
    print(f"✅  Added {len(new_rows)} new internship(s) to internships.csv.\n")
    rebuild_readme()

    # ── Summary ───────────────────────────────────────────────────────────────
    by_source: dict = {}
    for r in new_rows:
        src = r.get("_source", "?")
        by_source.setdefault(src, []).append(r)

    print("\n── New listings by source ─────────────────────────────────")
    for src, rows in by_source.items():
        print(f"\n  [{src}]")
        for r in rows:
            print(f"    • {r['company']} — {r['role']} [{r['term']}, {r['field']}]")
    print()


if __name__ == "__main__":
    main()
