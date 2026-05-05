# Internship Tracker — Changes & Deployment Guide

## What Changed and Why

---

### 1. Two New Job Sources Added

**Problem:** The original three sources (Adzuna, Jobicy, CSA) missed the vast majority of Canadian co-op postings because most companies don't post to general aggregators — they post directly to their own hiring portals.

---

#### Source 4: Indeed RSS Feeds

**File:** `fetch_internships.py` → `fetch_indeed()`

Indeed exposes a free, unauthenticated RSS feed at `ca.indeed.com/rss` that returns structured XML. No account or API key is needed. The script queries it with 10 different search terms (software intern, machine learning intern, aerospace intern, etc.) filtered to Canada, and parses the results using Python's built-in `xml.etree.ElementTree` — which was already imported in the original code but never used.

Each result is filtered through the same `is_intern_or_coop()` and `is_recent()` checks as other sources, so no junk slips through.

---

#### Source 5: ATS Portals (Greenhouse & Lever)

**File:** `fetch_internships.py` → `fetch_ats_portals()`, `fetch_greenhouse()`, `fetch_lever()`

Many well-known Canadian tech companies — Shopify, Cohere, 1Password, Wealthsimple, MDA Space, and others — post internships *exclusively* on their own Applicant Tracking System (ATS) pages. These postings never appear on Indeed or Adzuna.

Both Greenhouse and Lever expose free, public JSON APIs:

- **Greenhouse:** `https://boards-api.greenhouse.io/v1/boards/{company-slug}/jobs`
- **Lever:** `https://api.lever.co/v0/postings/{company-slug}?mode=json`

The script queries 15 hardcoded Canadian companies across both platforms. To add more companies, find their slug (the short name in their careers URL, e.g. `shopify` from `greenhouse.io/boards/shopify`) and add a line to the `ATS_COMPANIES` list at the top of that section.

**Companies currently tracked:**
Shopify, Cohere, Wealthsimple, Lightspeed Commerce, D2L, Vidyard, Hootsuite, Kinaxis, OpenText, MDA Space, Miovision, Genetec, 1Password, Faire, Properly

---

### 2. Smarter Deduplication (Fuzzy Matching)

**File:** `fetch_internships.py` → `is_fuzzy_duplicate()`

**Problem:** The original deduplication did an exact string match on `(company, role)`. This meant "Software Engineer Intern" and "Software Engineering Intern (Co-op)" from the same company would both be saved as separate entries even though they are the same posting.

**Fix:** The new deduplication uses Python's built-in `difflib.SequenceMatcher` to compare company names and role titles. If both strings score above a similarity threshold of `0.82` (out of 1.0), the entry is treated as a duplicate and skipped. No new dependencies are needed — `difflib` is part of the Python standard library.

You can tune the threshold by changing `FUZZY_CUTOFF` at the top of the file. Higher values = stricter matching (fewer false positives but may miss real duplicates). Lower values = looser matching (catches more duplicates but risks incorrectly dropping distinct postings).

---

### 3. Automatic Expiry of Old Listings

**File:** `fetch_internships.py` → `purge_old_entries()`

**Problem:** The original script only appended new rows — it never removed old ones. After a few months the CSV would accumulate hundreds of expired postings from past terms, making it useless as an active reference.

**Fix:** Every run now loads the full existing CSV, removes any row whose `date_posted` is older than `MAX_AGE_DAYS` (60 days by default), then rewrites the whole file. The script prints how many listings were purged each run. Adjust `MAX_AGE_DAYS` at the top of the file if you want a longer or shorter window.

---

### 4. Salary Data Captured from Adzuna

**File:** `fetch_internships.py` → `format_salary()`, CSV fieldnames

**Problem:** The original script fetched `salary_min` and `salary_max` from Adzuna's API response but silently dropped them. The CSV had no salary column at all.

**Fix:** A `salary` column has been added to the CSV. When Adzuna returns salary data, it is formatted as a human-readable range (e.g. `$45,000–$60,000`). Other sources leave the field blank since they don't expose salary information. The column is the last before `notes` so it doesn't break existing tooling that reads the first several columns.

---

### 5. Summer Term Added to Term Inference

**File:** `fetch_internships.py` → `infer_term()`

**Problem:** The original `infer_term()` function only recognised Winter and Fall terms. It defaulted to Fall for any posting from January to June, which misclassified all May–August summer internship postings.

**Fix:** Summer is now a recognised term. The keyword list checks for "summer", "may", "june", "july", "august", and short forms like "s25". The month-based fallback now maps Jan–Apr → Summer, May–Aug → Fall, Sep–Dec → Winter, which better reflects when companies typically post for each upcoming term.

---

### 6. More Granular Duration Inference

**File:** `fetch_internships.py` → `infer_duration()`

**Problem:** The original function only detected 8-month terms and defaulted everything else to 4. This missed 6-month, 12-month, and 16-month placements.

**Fix:** Duration inference now uses `re.search()` with patterns for 6, 8, 12, and 16 months. The patterns handle both numeric ("12-month") and written ("twelve month") forms.

---

### 7. Retry Logic on HTTP Requests

**File:** `fetch_internships.py` → `fetch_url()`

**Problem:** A single transient network error — a timeout, a momentary 5xx from a job board — would silently skip an entire source with no retry. When running in GitHub Actions on a shared runner, transient failures are common.

**Fix:** `fetch_url()` now retries up to 3 times with exponential backoff (waits 1s, then 2s, then 4s between attempts). It prints a message on each retry attempt so you can see what happened in the workflow logs. The behaviour on permanent failures (e.g. a source that is actually down) is unchanged — it skips that source and continues.

---

### 8. Robust HTML Parsing for the CSA Page

**File:** `fetch_internships.py` → `fetch_csa()`

**Problem:** The original CSA scraper used a hand-rolled regex against raw HTML markup. Government websites restructure their pages periodically, and any change to the HTML would silently break the regex and return zero results.

**Fix:** The scraper now uses Python's built-in `html.parser` via a small `LinkParser` class that subclasses `HTMLParser`. This is far more resilient to whitespace changes, attribute reordering, and minor markup edits. It extracts anchor tags and their text content, then applies the same keyword and length filters as before.

---

### 9. Failure Notification via GitHub Issues

**File:** `refresh.yml` → `Notify on failure` step

**Problem:** If the workflow crashed, you would only know if you happened to check the GitHub Actions tab. There was no alerting.

**Fix:** A final step in the workflow runs only on failure (`if: failure()`). It uses the `github-script` action to automatically open a GitHub Issue in the same repo, titled with the date and linking directly to the failed run. This means you get a notification in your GitHub inbox whenever the weekly refresh breaks, without needing to set up external services like Slack or email.

---

## Deployment Guide — Putting This on GitHub

### Step 1: Set up your repository structure

Your repo should look like this:

```
your-repo/
├── .github/
│   └── workflows/
│       └── refresh.yml          ← the workflow file
├── scripts/
│   ├── fetch_internships.py     ← the main fetcher
│   └── update_readme.py         ← your existing README builder
├── internships.csv              ← auto-generated, commit an empty one to start
└── README.md                    ← auto-generated
```

If you don't have an `internships.csv` yet, create an empty one with just the header row:

```
company,role,location,term,duration,field,link,date_posted,deadline,salary,notes
```

### Step 2: Add the files to the repo

```bash
# Clone your repo if you haven't already
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO

# Create the folder structure
mkdir -p .github/workflows scripts

# Copy the two files into the right places
cp fetch_internships.py scripts/fetch_internships.py
cp refresh.yml .github/workflows/refresh.yml

# Create an empty CSV with the header
echo "company,role,location,term,duration,field,link,date_posted,deadline,salary,notes" > internships.csv

# Stage, commit, and push
git add .
git commit -m "feat: add internship fetcher and workflow"
git push
```

### Step 3: Add Adzuna API credentials (optional but recommended)

Adzuna is free. Get a key at https://developer.adzuna.com, then:

1. Go to your repo on GitHub
2. Click **Settings** → **Secrets and variables** → **Actions**
3. Click **New repository secret** and add two secrets:
   - Name: `ADZUNA_APP_ID` — Value: your app ID
   - Name: `ADZUNA_APP_KEY` — Value: your app key

If you skip this step, Adzuna is simply skipped and the other four sources still run.

### Step 4: Run it manually the first time

1. Go to your repo → **Actions** tab
2. Click **Refresh Internship Listings** in the left sidebar
3. Click **Run workflow** → **Run workflow**

Watch the logs to confirm all sources are returning results. The first run will populate `internships.csv` and rebuild `README.md`.

### Step 5: Let it run automatically

After the first successful run, the workflow fires every Monday at 9:00 AM UTC automatically. You don't need to do anything. If it ever fails, a GitHub Issue will be opened in your repo to alert you.

---

## Customisation Reference

| What you want to change | Where to change it |
|---|---|
| How old listings can be before being purged | `MAX_AGE_DAYS` at the top of `fetch_internships.py` |
| How strict the duplicate detection is | `FUZZY_CUTOFF` at the top of `fetch_internships.py` (0.0–1.0) |
| Add a new company on Greenhouse | Add a row to `ATS_COMPANIES` with `"greenhouse"` and the company's board slug |
| Add a new company on Lever | Add a row to `ATS_COMPANIES` with `"lever"` and the company's board slug |
| Add a new Indeed search query | Add a row to `INDEED_SEARCHES` |
| Change the schedule | Edit the `cron` value in `refresh.yml` |
| Run it on a different day/time | `0 9 * * 1` = Monday 9am UTC. Change `1` to `2` for Tuesday, etc. |
