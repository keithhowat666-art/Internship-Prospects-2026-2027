"""
fetch_internships.py
--------------------
Pulls Canadian STEM internships from five sources:

  1. Adzuna API           — broad Canadian job board aggregator (optional, free key)
  2. Jobicy API           — remote-friendly tech roles, Canada filter, no key needed
  3. Canadian Space Agency — official CSA internship page (government HTML)
  4. Indeed RSS feeds     — unauthenticated RSS search results (no key needed)
  5. ATS portals          — direct Greenhouse / Lever career pages for known Canadian tech companies

Deduplicates all results (fuzzy match on company+role) against internships.csv,
purges entries older than MAX_AGE_DAYS, appends new entries, and rebuilds README.md.

Usage:
    python scripts/fetch_internships.py

Environment variables required (for Adzuna only):
    ADZUNA_APP_ID    — https://developer.adzuna.com (free)
    ADZUNA_APP_KEY   — https://developer.adzuna.com (free)

All other sources need no credentials.
"""

import csv
import difflib
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

MAX_AGE_DAYS   = 60
FUZZY_CUTOFF   = 0.82   # SequenceMatcher ratio threshold for duplicate detection

CSV_FIELDNAMES = [
    "company", "role", "location", "term", "duration",
    "field", "link", "date_posted", "deadline", "salary", "notes",
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
    if any(kw in text for kw in ["summer", "may", "june", "july", "august", "s25", "s26"]):
        return "Summer"
    # Infer from current month: Jan–Apr → Summer, May–Aug → Fall, Sep–Dec → Winter
    m = date.today().month
    if m <= 4:   return "Summer"
    if m <= 8:   return "Fall"
    return "Winter"

def infer_duration(title: str, description: str) -> str:
    text = (title + " " + description).lower()
    for pat, val in [
        (r"\b(12|twelve)[\s-]month",  "12"),
        (r"\b(16|sixteen)[\s-]month", "16"),
        (r"\b(8|eight)[\s-]month",    "8"),
        (r"\btwo[\s-]term\b",         "8"),
        (r"\bdouble[\s-]term\b",      "8"),
        (r"\b(6|six)[\s-]month",      "6"),
    ]:
        if re.search(pat, text):
            return val
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

def format_salary(min_val, max_val) -> str:
    """Format Adzuna salary fields into a human-readable range string."""
    try:
        lo = int(float(min_val)) if min_val else None
        hi = int(float(max_val)) if max_val else None
        if lo and hi:
            return f"${lo:,}–${hi:,}"
        if lo:
            return f"${lo:,}+"
        if hi:
            return f"Up to ${hi:,}"
    except Exception:
        pass
    return ""

# ── HTTP helper with retry ────────────────────────────────────────────────────

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; internship-tracker/1.0)"}

def fetch_url(url: str, timeout: int = 15, retries: int = 3) -> bytes | None:
    """Fetch a URL with exponential-backoff retry on failure."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:
            wait = 2 ** attempt
            if attempt < retries - 1:
                print(f"  ⚠️  Fetch failed (attempt {attempt+1}/{retries}): {url[:80]}  ({e}) — retrying in {wait}s")
                time.sleep(wait)
            else:
                print(f"  ⚠️  Fetch failed after {retries} attempts: {url[:80]}  ({e})")
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
            sal     = format_salary(job.get("salary_min"), job.get("salary_max"))

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
                "salary":      sal,
                "notes":       "",
                "_source":     "Adzuna",
            })
            added += 1
        print(f"     {added} result(s)")

    return results


# ── Source 2: Jobicy (free API, no key needed) ────────────────────────────────

JOBICY_SEARCHES = [
    ("intern",  "dev"),
    ("co-op",   "dev"),
    ("intern",  "data-science"),
    ("intern",  "engineering"),
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
                "salary":      "",
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

    # Parse with html.parser for robustness against markup changes
    from html.parser import HTMLParser

    class LinkParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links: list[tuple[str, str]] = []
            self._current_href = ""
            self._capture = False
            self._buf = ""

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                attrs_dict = dict(attrs)
                self._current_href = attrs_dict.get("href", "")
                self._capture = True
                self._buf = ""

        def handle_endtag(self, tag):
            if tag == "a" and self._capture:
                self.links.append((self._current_href, self._buf.strip()))
                self._capture = False

        def handle_data(self, data):
            if self._capture:
                self._buf += data

    parser = LinkParser()
    parser.feed(text)

    results = []
    seen_titles: set = set()

    for href, raw_title in parser.links:
        title = html.unescape(raw_title).strip()

        if len(title) < 15:
            continue
        if any(nav in title.lower() for nav in [
            "back to", "home", "contact", "français", "english", "skip", "menu"
        ]):
            continue
        if not any(kw in title.lower() for kw in [
            "intern", "stage", "co-op", "student", "analyst", "engineer",
            "developer", "scientist", "technician", "research",
        ]):
            continue
        if title in seen_titles:
            continue
        seen_titles.add(title)

        if href.startswith("/"):
            href = "https://www.asc-csa.gc.ca" + href
        elif not href.startswith("http"):
            href = CSA_URL

        results.append({
            "company":     "Canadian Space Agency",
            "role":        title,
            "location":    "Longueuil, QC",
            "term":        infer_term(title, ""),
            "duration":    infer_duration(title, ""),
            "field":       infer_field(title, "", "Aerospace"),
            "link":        href,
            "date_posted": date.today().isoformat(),
            "deadline":    "",
            "salary":      "",
            "notes":       "Apply via co-op office",
            "_source":     "CSA",
        })

    print(f"     {len(results)} result(s)")
    return results


# ── Source 4: Indeed RSS feeds ────────────────────────────────────────────────

INDEED_SEARCHES = [
    ("software+intern",               "Software"),
    ("software+developer+co-op",      "Software"),
    ("machine+learning+intern",       "AI / Data"),
    ("data+science+intern",           "AI / Data"),
    ("mechanical+engineering+co-op",  "Engineering"),
    ("electrical+engineering+intern", "Engineering"),
    ("aerospace+intern",              "Aerospace"),
    ("biotech+intern",                "Biotech"),
    ("research+assistant+intern",     "Research / Physics"),
    ("summer+student+STEM",           "Software"),
]

def fetch_indeed() -> list[dict]:
    """
    Parse Indeed's unauthenticated RSS feed for Canadian internship listings.
    No API key required. Returns Atom/RSS XML parsed with stdlib xml.etree.
    """
    print("\n📡  Source 4: Indeed RSS feeds")
    results = []

    for query, field_hint in INDEED_SEARCHES:
        params = urllib.parse.urlencode({
            "q":          query.replace("+", " "),
            "l":          "Canada",
            "sort":       "date",
            "fromage":    str(MAX_AGE_DAYS),
            "limit":      25,
        })
        url = f"https://ca.indeed.com/rss?{params}"
        print(f"  → {query.replace('+', ' ')!r}")
        raw = fetch_url(url)
        time.sleep(0.5)
        if not raw:
            continue

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            print(f"     XML parse error: {e}")
            continue

        # RSS namespace map (Indeed uses standard RSS 2.0, no namespace prefix needed)
        ns = {"dc": "http://purl.org/dc/elements/1.1/"}
        items = root.findall(".//item")

        added = 0
        for item in items:
            title   = (item.findtext("title") or "").strip()
            link    = (item.findtext("link")  or "").strip()
            desc    = (item.findtext("description") or "").strip()
            company = (item.findtext("dc:creator", namespaces=ns) or "").strip()
            pub     = (item.findtext("pubDate") or "").strip()
            loc_raw = (item.findtext("location") or "Canada").strip()

            # Strip HTML tags from description
            desc = re.sub(r"<[^>]+>", " ", desc)

            if not is_intern_or_coop(title, desc): continue
            if not title or not link:              continue

            # pubDate in Indeed RSS is RFC 2822 — parse it gracefully
            date_posted = date.today().isoformat()
            if pub:
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(pub)
                    if (datetime.now(dt.tzinfo) - dt).days > MAX_AGE_DAYS:
                        continue
                    date_posted = dt.strftime("%Y-%m-%d")
                except Exception:
                    pass

            results.append({
                "company":     company or "Unknown",
                "role":        title,
                "location":    clean_location(loc_raw),
                "term":        infer_term(title, desc),
                "duration":    infer_duration(title, desc),
                "field":       infer_field(title, desc, field_hint),
                "link":        link,
                "date_posted": date_posted,
                "deadline":    "",
                "salary":      "",
                "notes":       "",
                "_source":     "Indeed",
            })
            added += 1

        print(f"     {added} result(s)")

    return results


# ── Source 5: ATS portals (Greenhouse / Lever) ────────────────────────────────

# Known Canadian tech/STEM companies and their ATS board slugs.
# Format: (display_name, ats_type, board_slug, field_hint)
ATS_COMPANIES = [
    # Greenhouse boards — API: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
    ("Shopify",              "greenhouse", "shopify",         "Software"),
    ("Cohere",               "greenhouse", "cohere",          "AI / Data"),
    ("Wealthsimple",         "greenhouse", "wealthsimple",    "Software"),
    ("Lightspeed Commerce",  "greenhouse", "lightspeedpos",   "Software"),
    ("D2L",                  "greenhouse", "d2l",             "Software"),
    ("Vidyard",              "greenhouse", "vidyard",         "Software"),
    ("Hootsuite",            "greenhouse", "hootsuite",       "Software"),
    ("Kinaxis",              "greenhouse", "kinaxis",         "Software"),
    ("OpenText",             "greenhouse", "opentext",        "Software"),
    ("MDA Space",            "greenhouse", "mdaspace",        "Aerospace"),
    ("Miovision",            "greenhouse", "miovision",       "Software"),
    ("Genetec",              "greenhouse", "genetec",         "Software"),
    # Lever boards — API: https://api.lever.co/v0/postings/{slug}?mode=json
    ("1Password",            "lever",      "1password",       "Software"),
    ("Faire",                "lever",      "faire",           "Software"),
    ("Properly",             "lever",      "properly",        "Software"),
]

def fetch_greenhouse(company: str, slug: str, field_hint: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    raw = fetch_url(url)
    if not raw:
        return []
    try:
        jobs = json.loads(raw).get("jobs", [])
    except Exception:
        return []

    results = []
    for job in jobs:
        title = (job.get("title") or "").strip()
        link  = (job.get("absolute_url") or "").strip()
        loc   = clean_location((job.get("location") or {}).get("name", "Canada"))
        desc  = re.sub(r"<[^>]+>", " ", job.get("content") or "")
        updated = job.get("updated_at") or ""

        if not is_intern_or_coop(title, desc): continue
        if not is_recent(updated):             continue

        results.append({
            "company":     company,
            "role":        title,
            "location":    loc,
            "term":        infer_term(title, desc),
            "duration":    infer_duration(title, desc),
            "field":       infer_field(title, desc, field_hint),
            "link":        link,
            "date_posted": parse_date(updated),
            "deadline":    "",
            "salary":      "",
            "notes":       "",
            "_source":     "ATS/Greenhouse",
        })
    return results

def fetch_lever(company: str, slug: str, field_hint: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    raw = fetch_url(url)
    if not raw:
        return []
    try:
        jobs = json.loads(raw)
        if not isinstance(jobs, list):
            return []
    except Exception:
        return []

    results = []
    for job in jobs:
        title = (job.get("text") or "").strip()
        link  = (job.get("hostedUrl") or "").strip()
        loc   = clean_location((job.get("categories") or {}).get("location", "Canada"))
        desc  = " ".join(
            block.get("text", "")
            for block in (job.get("descriptionPlain") or [])
            if isinstance(block, dict)
        ) if isinstance(job.get("descriptionPlain"), list) else (job.get("descriptionPlain") or "")
        created_ts = job.get("createdAt", 0)

        if not is_intern_or_coop(title, str(desc)): continue

        # Lever timestamps are milliseconds
        try:
            created_dt = datetime.utcfromtimestamp(created_ts / 1000)
            if (datetime.utcnow() - created_dt).days > MAX_AGE_DAYS:
                continue
            date_posted = created_dt.strftime("%Y-%m-%d")
        except Exception:
            date_posted = date.today().isoformat()

        results.append({
            "company":     company,
            "role":        title,
            "location":    loc,
            "term":        infer_term(title, str(desc)),
            "duration":    infer_duration(title, str(desc)),
            "field":       infer_field(title, str(desc), field_hint),
            "link":        link,
            "date_posted": date_posted,
            "deadline":    "",
            "salary":      "",
            "notes":       "",
            "_source":     "ATS/Lever",
        })
    return results

def fetch_ats_portals() -> list[dict]:
    print("\n📡  Source 5: ATS portals (Greenhouse / Lever)")
    results = []
    for company, ats_type, slug, field_hint in ATS_COMPANIES:
        print(f"  → {company} ({ats_type})")
        if ats_type == "greenhouse":
            found = fetch_greenhouse(company, slug, field_hint)
        else:
            found = fetch_lever(company, slug, field_hint)
        print(f"     {len(found)} intern/co-op posting(s)")
        results.extend(found)
        time.sleep(0.4)
    return results


# ── Fuzzy deduplication ───────────────────────────────────────────────────────

def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()

def is_fuzzy_duplicate(row: dict, existing_keys: list[tuple[str, str]]) -> bool:
    """Return True if (company, role) fuzzy-matches any existing key."""
    c = row["company"].strip().lower()
    r = row["role"].strip().lower()
    for ec, er in existing_keys:
        if _similarity(c, ec) >= FUZZY_CUTOFF and _similarity(r, er) >= FUZZY_CUTOFF:
            return True
    return False


# ── CSV helpers ───────────────────────────────────────────────────────────────

def load_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def purge_old_entries(rows: list[dict]) -> tuple[list[dict], int]:
    """Remove entries older than MAX_AGE_DAYS. Returns (kept_rows, removed_count)."""
    cutoff = date.today() - timedelta(days=MAX_AGE_DAYS)
    kept, removed = [], 0
    for row in rows:
        try:
            posted = date.fromisoformat(row.get("date_posted", ""))
            if posted < cutoff:
                removed += 1
                continue
        except ValueError:
            pass  # keep rows with unparseable dates
        kept.append(row)
    return kept, removed

def write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDNAMES})

def rebuild_readme() -> None:
    script = ROOT / "scripts" / "update_readme.py"
    if not script.exists():
        print("  ⚠️  update_readme.py not found — skipping README rebuild.")
        return
    result = subprocess.run(
        [sys.executable, str(script)],
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
    all_found += fetch_indeed()
    all_found += fetch_ats_portals()

    print(f"\n📋  Total found across all sources: {len(all_found)}")

    # ── Load existing CSV and purge stale entries ─────────────────────────────
    existing_rows = load_existing(CSV_PATH)
    existing_rows, purged = purge_old_entries(existing_rows)
    if purged:
        print(f"🗑️   Purged {purged} listing(s) older than {MAX_AGE_DAYS} days.")

    existing_keys = [
        (r["company"].strip().lower(), r["role"].strip().lower())
        for r in existing_rows
    ]

    # ── Deduplicate (fuzzy) ───────────────────────────────────────────────────
    seen_in_run: list[tuple[str, str]] = []
    new_rows: list[dict] = []

    for row in all_found:
        if is_fuzzy_duplicate(row, existing_keys + seen_in_run):
            continue
        new_rows.append(row)
        seen_in_run.append((row["company"].strip().lower(), row["role"].strip().lower()))

    if not new_rows and not purged:
        print("✅  No new internships found — CSV is already up to date.")
        return

    # ── Save ──────────────────────────────────────────────────────────────────
    all_rows = existing_rows + new_rows
    write_csv(CSV_PATH, all_rows)
    print(f"✅  Added {len(new_rows)} new internship(s) to internships.csv.")
    print(f"📄  Total listings in CSV: {len(all_rows)}")

    rebuild_readme()

    # ── Summary ───────────────────────────────────────────────────────────────
    if new_rows:
        by_source: dict = {}
        for r in new_rows:
            src = r.get("_source", "?")
            by_source.setdefault(src, []).append(r)

        print("\n── New listings by source ─────────────────────────────────")
        for src, rows in by_source.items():
            print(f"\n  [{src}]")
            for r in rows:
                sal = f"  💰 {r['salary']}" if r.get("salary") else ""
                print(f"    • {r['company']} — {r['role']} [{r['term']}, {r['field']}]{sal}")
        print()


if __name__ == "__main__":
    main()
