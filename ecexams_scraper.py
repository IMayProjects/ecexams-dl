#!/usr/bin/env python3
"""
ECExams Paper Scraper
=====================
Downloads exam papers and memos from https://www.ecexams.co.za/ExaminationPapers.htm
and organises them into a folder hierarchy:

    downloads/
    └── Grade 12/
        └── 2024 - November NSC Grade 12 Examinations/
            ├── Mathematics_P1.pdf
            ├── Mathematics_P1_Memo.pdf
            └── ...

Usage:
    python ecexams_scraper.py                        # Download everything
    python ecexams_scraper.py --grade 12             # Only Grade 12
    python ecexams_scraper.py --year 2024            # Only 2024 papers
    python ecexams_scraper.py --grade 12 --year 2024 # Grade 12, 2024 only
    python ecexams_scraper.py --dry-run              # Show what would be downloaded
    python ecexams_scraper.py --threads 5            # Parallel downloads (default: 3)
    python ecexams_scraper.py --output-dir my_papers # Custom output folder

Requirements:
    pip install requests beautifulsoup4 lxml
"""

import argparse
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ─── Config ──────────────────────────────────────────────────────────────────

BASE_URL = "https://www.ecexams.co.za/"
INDEX_URL = "https://www.ecexams.co.za/ExaminationPapers.htm"
DOWNLOAD_DIR = Path("downloads")
DELAY_BETWEEN_REQUESTS = 0.5   # seconds – be polite to the server
REQUEST_TIMEOUT = 30           # seconds
MAX_RETRIES = 3
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ECExamsScraper/1.0; "
        "+https://github.com/your-username/ecexams-scraper)"
    )
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def safe_get(session: requests.Session, url: str):
    """GET with retries and polite delay. Returns Response or None."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(DELAY_BETWEEN_REQUESTS)
            r = session.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            log.warning(f"Attempt {attempt}/{MAX_RETRIES} failed for {url}: {e}")
            if attempt == MAX_RETRIES:
                log.error(f"Giving up on {url}")
                return None
            time.sleep(2 ** attempt)  # exponential back-off
    return None


def sanitise(name: str) -> str:
    """Strip characters that are illegal in folder/file names."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120]  # cap length


def detect_grade(text: str) -> str:
    """Return a grade label like 'Grade 12' from a heading / link text."""
    text_lower = text.lower()

    m = re.search(r"gr(?:ade)?\.?\s*(\d+)", text_lower)
    if m:
        return f"Grade {m.group(1)}"

    if "gec" in text_lower or "general education certificate" in text_lower:
        return "Grade 9 (GEC)"

    if "annual national assessment" in text_lower or " ana" in text_lower:
        return "ANA (Grades 1-6 & 9)"

    return "Other"


def detect_year(text: str) -> str:
    m = re.search(r"\b(20\d{2})\b", text)
    return m.group(1) if m else "Unknown Year"


# ─── Step 1 – scrape the index page ──────────────────────────────────────────

def scrape_index(session, grade_filter, year_filter):
    """
    Returns a list of exam-session dicts:
        {
            'url': 'https://...',
            'title': '2024 - November NSC Grade 12 Examinations',
            'grade': 'Grade 12',
            'year': '2024',
        }
    """
    log.info(f"Fetching index: {INDEX_URL}")
    r = safe_get(session, INDEX_URL)
    if r is None:
        raise RuntimeError("Could not fetch the index page.")

    soup = BeautifulSoup(r.text, "lxml")
    sessions = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()

        # Skip external / mailto / anchor links and non-.htm targets
        if href.startswith(("mailto:", "http://bit.ly", "https://bit.ly", "#")):
            continue
        if not href.endswith(".htm"):
            continue

        full_url = urljoin(BASE_URL, href)
        if full_url == INDEX_URL:
            continue

        link_text = a.get_text(separator=" ", strip=True)
        if not link_text or len(link_text) < 5:
            continue

        year_from_url = detect_year(href)
        year_from_text = detect_year(link_text)
        year = year_from_text if year_from_text != "Unknown Year" else year_from_url

        grade = detect_grade(link_text + " " + href)
        title = f"{sanitise(link_text)}"

        if grade_filter and grade_filter not in grade:
            continue
        if year_filter and year_filter != year:
            continue

        # Deduplicate
        if any(s["url"] == full_url for s in sessions):
            continue

        sessions.append({
            "url": full_url,
            "title": title,
            "grade": grade,
            "year": year,
        })

    log.info(f"Found {len(sessions)} exam session(s) matching filters.")
    return sessions


# ─── Step 2 – scrape a session sub-page ──────────────────────────────────────

def scrape_session_page(session, exam_session):
    """
    Returns a list of file dicts:
        {
            'url': 'https://...pdf',
            'filename': 'Mathematics_P1.pdf',
            'exam_session': {...},
        }
    """
    url = exam_session["url"]
    log.info(f"  Scanning: {url}")
    r = safe_get(session, url)
    if r is None:
        return []

    soup = BeautifulSoup(r.text, "lxml")
    files = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue

        ext = Path(urlparse(href).path).suffix.lower()
        if ext not in (".pdf", ".zip", ".docx"):
            continue

        file_url = urljoin(url, href)
        if file_url in seen_urls:
            continue
        seen_urls.add(file_url)

        raw_name = a.get_text(separator=" ", strip=True)
        if not raw_name:
            raw_name = Path(urlparse(href).path).stem

        clean_name = sanitise(raw_name)
        filename = clean_name if clean_name.lower().endswith(ext) else clean_name + ext

        files.append({
            "url": file_url,
            "filename": filename,
            "exam_session": exam_session,
        })

    log.info(f"    → {len(files)} file(s) found")
    return files


# ─── Step 3 – download a single file ─────────────────────────────────────────

def download_file(session, file_info, dry_run):
    """Download one file. Returns a result dict."""
    es = file_info["exam_session"]
    dest_dir = DOWNLOAD_DIR / sanitise(es["grade"]) / es["year"] / sanitise(es["title"])
    dest_path = dest_dir / file_info["filename"]

    result = {"url": file_info["url"], "path": str(dest_path), "status": None}

    if dry_run:
        result["status"] = "dry-run"
        log.info(f"[DRY-RUN] → {dest_path}")
        return result

    if dest_path.exists():
        result["status"] = "skipped (exists)"
        return result

    dest_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"  ↓  {dest_path}")
    r = safe_get(session, file_info["url"])
    if r is None:
        result["status"] = "failed"
        return result

    dest_path.write_bytes(r.content)
    result["status"] = "downloaded"
    return result


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Download ECExams papers & memos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--grade", help="Filter by grade number, e.g. '12'")
    parser.add_argument("--year", help="Filter by year, e.g. '2024'")
    parser.add_argument("--dry-run", action="store_true",
                        help="List files without downloading anything")
    parser.add_argument("--threads", type=int, default=3,
                        help="Parallel download threads (default: 3)")
    parser.add_argument("--output-dir", default="downloads",
                        help="Root download folder (default: downloads/)")
    args = parser.parse_args()

    global DOWNLOAD_DIR
    DOWNLOAD_DIR = Path(args.output_dir)

    grade_filter = f"Grade {args.grade}" if args.grade else None
    year_filter = args.year

    http = get_session()

    # 1. Index
    exam_sessions = scrape_index(http, grade_filter, year_filter)
    if not exam_sessions:
        log.warning("No matching exam sessions found. Check your --grade / --year filters.")
        return

    # 2. Sub-pages → collect all file links
    all_files = []
    for es in exam_sessions:
        files = scrape_session_page(http, es)
        all_files.extend(files)

    if not all_files:
        log.warning("No downloadable files found.")
        return

    log.info(f"\nTotal files to process: {len(all_files)}")
    if args.dry_run:
        log.info("DRY-RUN mode – no files will be written to disk.\n")

    # 3. Download (parallel)
    downloaded = skipped = failed = dry_count = 0

    def _task(fi):
        return download_file(http, fi, dry_run=args.dry_run)

    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        futures = {pool.submit(_task, fi): fi for fi in all_files}
        for future in as_completed(futures):
            result = future.result()
            s = result["status"]
            if s == "downloaded":
                downloaded += 1
            elif s == "failed":
                failed += 1
                log.error(f"Failed: {result['url']}")
            elif s == "dry-run":
                dry_count += 1
            else:
                skipped += 1

    print("\n" + "=" * 55)
    if args.dry_run:
        print(f"  Would download : {dry_count} file(s)")
    else:
        print(f"  Downloaded     : {downloaded}")
        print(f"  Skipped        : {skipped}  (already existed)")
        print(f"  Failed         : {failed}")
        print(f"  Output dir     : {DOWNLOAD_DIR.resolve()}")
    print("=" * 55)


if __name__ == "__main__":
    main()
