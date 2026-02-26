# How the ECExams Scraper Works — A Rebuilder's Guide

This document explains the architecture, design decisions, and implementation details of the ECExams Scraper in enough depth that you could rebuild it from scratch. It covers the scraping strategy, the web UI, the real-time streaming mechanism, and the tradeoffs made along the way.

---

## Table of contents

1. [The target site](#1-the-target-site)
2. [Scraping strategy](#2-scraping-strategy)
3. [Core scraper logic](#3-core-scraper-logic)
4. [CLI interface](#4-cli-interface)
5. [Web UI architecture](#5-web-ui-architecture)
6. [Real-time streaming with SSE](#6-real-time-streaming-with-sse)
7. [Concurrency model](#7-concurrency-model)
8. [File organisation](#8-file-organisation)
9. [Error handling and politeness](#9-error-handling-and-politeness)
10. [Frontend design](#10-frontend-design)
11. [Putting it all together](#11-putting-it-all-together)

---

## 1. The target site

**URL:** https://www.ecexams.co.za/ExaminationPapers.htm

The site is a static HTML site with no JavaScript rendering, no authentication, and no API. It has a two-level structure:

**Level 1 — Index page**
A single page listing all available exam sessions as plain `<a href="...htm">` links. Each link points to a session sub-page. The links are grouped loosely by grade, but the page has no semantic structure (no `<section>`, `<article>`, or `id` attributes) — it is entirely table-based HTML from roughly the early 2000s.

**Level 2 — Session sub-pages**
Each session sub-page (e.g. `2024_November_Gr_12_NSC_DBE_Exams.htm`) contains a list of downloadable files, again as plain `<a>` links, pointing directly to `.pdf`, `.zip`, or `.docx` files.

Because the site is entirely static, a simple `requests` + `BeautifulSoup` approach is sufficient. No browser automation (Selenium, Playwright) is needed.

---

## 2. Scraping strategy

The scraper works in three sequential stages:

```
Stage 1: Fetch the index page
         → extract all session links (URLs, inferred grade, inferred year)
         → apply grade/year filters

Stage 2: For each session link, fetch the sub-page
         → extract all .pdf / .zip / .docx download links

Stage 3: Download each file
         → organise into Grade / Year / Session folder hierarchy
         → skip files that already exist
```

### Inferring grade and year

The site does not tag links with structured metadata. Grade and year are inferred from the link text and URL using regular expressions.

**Year** — extracted with `\b(20\d{2})\b`. First tried against the link text; if not found, tried against the URL path (which reliably contains the year for most sessions).

**Grade** — extracted with `gr(?:ade)?\.?\s*(\d+)` (case-insensitive). This matches "Grade 12", "Gr. 12", "Gr12", etc. Special cases are handled explicitly:
- Links containing "gec" or "general education certificate" → `Grade 9 (GEC)`
- Links containing "annual national assessment" or " ana" → `ANA (Grades 1-6 & 9)`
- Anything else → `Other`

---

## 3. Core scraper logic

The scraper core is identical between the CLI (`ecexams_scraper.py`) and the web UI (`app.py`) — the only difference is how progress is reported.

### `_get(session, url, emit=None)`

The central HTTP fetch function. Wraps `requests.Session.get()` with:
- A configurable delay (`DELAY = 0.4s`) before every request
- Up to `MAX_RETRY = 3` attempts
- Exponential back-off between retries (`2^attempt` seconds: 2s, 4s, 8s)
- Returns `None` on total failure rather than raising, so the caller can skip gracefully

```python
def _get(session, url, emit=None):
    for attempt in range(1, MAX_RETRY + 1):
        try:
            time.sleep(DELAY)
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if emit:
                emit("warn", f"Retry {attempt}/{MAX_RETRY} – {url}: {e}")
            if attempt == MAX_RETRY:
                return None
            time.sleep(2 ** attempt)
    return None
```

### `scrape_index(session, grade_filters, year_filters, emit)`

Fetches the index page, walks every `<a href>` tag, and builds a list of session dicts. Filters are lists of strings — an empty list means "no filter, accept all". A session dict looks like:

```python
{
    "url":   "https://www.ecexams.co.za/2024_November_Gr_12_NSC_DBE_Exams.htm",
    "title": "November NSC Grade 12 Examinations",
    "grade": "Grade 12",
    "year":  "2024",
}
```

### `scrape_session(session, exam_session, emit)`

Fetches a single session sub-page and returns a list of file dicts. Only `.pdf`, `.zip`, and `.docx` extensions are collected. A `seen` set deduplicates URLs within a page:

```python
{
    "url":          "https://www.ecexams.co.za/2024_Nov_Gr12/Maths_P1.pdf",
    "filename":     "Mathematics P1.pdf",
    "exam_session": { ... },
}
```

### `download_file(session, fi, root, dry_run, emit)`

Downloads a single file to `root/<grade>/<year>/<session>/<filename>`. Returns a string status: `"downloaded"`, `"skipped"`, `"failed"`, or `"dryrun"`. The caller aggregates these into counts.

---

## 4. CLI interface

The CLI (`ecexams_scraper.py`) uses `argparse` and `concurrent.futures.ThreadPoolExecutor` directly in `main()`.

The `emit` function for the CLI simply calls `logging.info` / `logging.warning` / `logging.error`.

```python
# Simplified flow:
sessions  = scrape_index(http, grade_filters, year_filters, emit)
all_files = [f for es in sessions for f in scrape_session(http, es, emit)]

with ThreadPoolExecutor(max_workers=args.threads) as pool:
    futures = {pool.submit(download_file, http, fi, root, dry_run, emit): fi
               for fi in all_files}
    for future in as_completed(futures):
        result = future.result()   # "downloaded" | "skipped" | "failed" | "dryrun"
```

---

## 5. Web UI architecture

`app.py` is a single-file Flask application. The HTML, CSS, and JavaScript are stored in a Python triple-quoted string (`HTML = r"""..."""`) and served via `render_template_string`. This keeps the project to a minimal set of files and avoids needing a `templates/` directory.

The Flask app has four routes:

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Serves the HTML UI |
| `/start` | POST | Accepts a JSON config payload, spawns the scraper in a background thread |
| `/stream` | GET | Server-Sent Events stream — pushes log messages to the browser |
| `/stop` | POST | Sets a flag to halt the background thread |

### Job state

A single global job is tracked with three variables:

```python
_job_lock   = threading.Lock()   # protects the variables below
_job_queue  = None               # queue.Queue — messages from scraper → SSE
_job_thread = None               # the background thread
_job_active = False              # whether a job is currently running
```

`/start` checks `_job_active` under the lock and returns HTTP 409 if a job is already running, preventing concurrent jobs.

---

## 6. Real-time streaming with SSE

The progress feed from the background scraper thread to the browser uses **Server-Sent Events (SSE)**, not WebSockets. SSE was chosen because:
- It is built into browsers natively (`EventSource` API), no library needed
- It is unidirectional (server → browser), which is all we need
- It works over a plain HTTP response with no upgrade handshake
- Flask supports it natively via `Response` with `mimetype="text/event-stream"`

### How it works

The background thread calls `emit(kind, msg)` which puts a dict onto a `queue.Queue`. The `/stream` route runs a generator that blocks on `queue.get(timeout=1.0)` and yields each message as an SSE frame:

```python
def generate():
    while True:
        try:
            msg = q.get(timeout=1.0)
            yield f"data: {json.dumps(msg)}\n\n"
            if msg["kind"] == "done":
                break
        except queue.Empty:
            yield 'data: {"kind":"ping"}\n\n'   # keep-alive
```

The `\n\n` double newline is required by the SSE specification to delimit events.

The browser receives each event in `es.onmessage`:

```javascript
es = new EventSource('/stream');
es.onmessage = (e) => {
    const { kind, msg } = JSON.parse(e.data);
    if (kind === 'ping') return;
    if (kind === 'progress') { updateProgress(...); return; }
    if (kind === 'done') { /* finalise UI */ es.close(); return; }
    appendLog(kind, msg);
};
```

### Message kinds

| Kind | Payload | UI behaviour |
|---|---|---|
| `info` | HTML string | Appended to log in blue |
| `scan` | Session title | Appended to log in purple |
| `download` | Filename | Appended to log in green |
| `warn` | Warning string | Appended to log in amber |
| `error` | Error string | Appended to log in red |
| `dryrun` | File path | Appended to log in grey |
| `progress` | JSON `{done, total}` | Updates progress bar and stat counters |
| `done` | JSON `{dl, skip, fail, dry}` | Final summary, closes EventSource |
| `ping` | — | Keep-alive, ignored by browser |

---

## 7. Concurrency model

The scraper uses two layers of concurrency:

**1. Background thread** — the entire scrape/download job runs in a `threading.Thread` so Flask can continue serving the `/stream` SSE endpoint and handle `/stop` requests concurrently.

**2. Thread pool for downloads** — within the background thread, `concurrent.futures.ThreadPoolExecutor` parallelises the actual file downloads. Scanning (fetching session sub-pages) is done sequentially to keep it easy to follow in the log.

```
Main thread:  Flask routes (/, /start, /stream, /stop)
                │
                └── Background thread: scrape_index → scrape_session (×N, serial)
                                           └── ThreadPoolExecutor: download_file (×N, parallel)
```

The `queue.Queue` is thread-safe, so the background thread and pool workers can all call `emit()` concurrently without locks.

---

## 8. File organisation

Downloaded files are saved to:

```
<output_dir>/<grade>/<year>/<session_title>/<filename>
```

Each path component passes through `_sanitise()` which:
- Strips characters illegal on Windows and Unix (`\ / * ? : " < > |`)
- Collapses internal whitespace
- Truncates to 120 characters

The year is stored separately in the session dict so it can be used as its own folder level without duplicating it in the session title.

`dest_dir.mkdir(parents=True, exist_ok=True)` creates the full path tree in one call and is idempotent.

---

## 9. Error handling and politeness

### Per-request retry

`_get()` retries up to 3 times with exponential back-off. If all retries fail it returns `None`. Callers treat `None` as a skip (for session pages) or a "failed" count increment (for downloads).

### Delay between requests

A `time.sleep(DELAY)` call at the top of every `_get()` ensures there is always at least 0.4 s between outgoing requests, even across threads. This is the simplest polite-scraping mechanism: no rate limiter needed, no token bucket, just a sleep.

### Idempotency

`download_file()` checks `dest.exists()` before downloading. This means:
- Re-running never re-downloads existing files
- A partial run (interrupted, or stopped via the UI) can be safely resumed
- The "skipped" count tells you how many files were already on disk

### Stop signal

The web UI's Stop button calls `/stop` which sets `_job_active = False`. The background thread checks this flag in the SSE generator loop — it does not interrupt running downloads mid-flight, but prevents new ones from starting and causes the stream to close cleanly. A more aggressive implementation could use `threading.Event` to interrupt the thread pool, but clean shutdown is sufficient for this use case.

---

## 10. Frontend design

The UI uses no frontend framework — plain HTML, CSS, and vanilla JavaScript.

**Layout** — a two-column CSS Grid: a fixed-width left panel for controls and a fluid right panel for the live log.

**Checkbox groups** — custom-styled using a hidden native `<input type="checkbox">` with a sibling `<span class="cb-box">` that is styled as the visual checkbox. The CSS `:checked` pseudo-class and the `~` sibling combinator drive the checked state:

```css
.cb-item input:checked ~ .cb-box {
    background: var(--accent);
    border-color: var(--accent);
}
.cb-item input:checked ~ .cb-box::after {
    /* renders the checkmark using a rotated border trick */
    content: "";
    border: 2px solid #000;
    border-top: none; border-left: none;
    transform: rotate(45deg) translateY(-1px);
}
```

**Real-time log** — each `appendLog()` call creates a `<div>` with a timestamp, a kind label, and the message. `body.scrollTop = body.scrollHeight` auto-scrolls to the latest entry. `animation: fadeIn` gives each new line a subtle entrance.

**Progress bar** — a `<div>` whose `width` style is updated as `(done / total * 100) + '%'` with a CSS `transition: width 0.3s ease` for smooth animation.

**Server-Sent Events** — the browser opens an `EventSource('/stream')` after starting a job. Because SSE is a persistent HTTP connection, Nginx or reverse proxies need `proxy_buffering off` and `X-Accel-Buffering: no` headers if deployed behind one — these are already set in Flask's response headers.

---

## 11. Putting it all together

If you were rebuilding this from scratch, here is the recommended order:

1. **Verify the site structure manually** — fetch the index page in a browser and inspect the DOM. Confirm the two-level link pattern.

2. **Build `_get()` first** — a robust retry-with-back-off HTTP function is the foundation. Test it against a few URLs before touching the parsing logic.

3. **Build `scrape_index()`** — write it to return a list of session dicts. Print them. Verify the grade/year inference looks right across a sample of links.

4. **Build `scrape_session()`** — verify it returns the right file URLs for one or two sessions before wiring up the download step.

5. **Build `download_file()`** — test `--dry-run` first; check the folder structure looks right before writing any files to disk.

6. **Wire up the CLI** — `argparse` + `ThreadPoolExecutor` + summary counts. Get the CLI working end-to-end.

7. **Add Flask** — wrap the same scraper functions with `run_job()`. The key insight is that `emit()` is the only interface between the scraper and the outside world — swap `logging.info` for `queue.put` and the rest follows.

8. **Add SSE streaming** — implement `/stream` as a generator. Test it with `curl -N http://localhost:5000/stream` before wiring up the browser.

9. **Build the frontend** — start with the form and the log panel. Add the progress bar last once the SSE plumbing is confirmed working.

10. **Add the checkbox filters** — both sides need updating: the frontend sends arrays, the backend accepts arrays and applies `any(gf in grade for gf in grade_filters)` rather than a single equality check.

---

*Built with Python 3.12, Flask 3.1, requests 2.32, BeautifulSoup4 4.14, lxml 6.0.*
