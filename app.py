#!/usr/bin/env python3
"""
ECExams Scraper Web Interface
Run with: python app.py  →  open http://localhost:5000
"""

import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template_string, request

# ─── Scraper core (inline) ────────────────────────────────────────────────────

BASE_URL   = "https://www.ecexams.co.za/"
INDEX_URL  = "https://www.ecexams.co.za/ExaminationPapers.htm"
DELAY      = 0.4
TIMEOUT    = 30
MAX_RETRY  = 3
HEADERS    = {"User-Agent": "Mozilla/5.0 (compatible; ECExamsScraper/1.0)"}


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


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


def _sanitise(name):
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120]


def _grade(text):
    t = text.lower()
    m = re.search(r"gr(?:ade)?\.?\s*(\d+)", t)
    if m:
        return f"Grade {m.group(1)}"
    if "gec" in t or "general education certificate" in t:
        return "Grade 9 (GEC)"
    if "annual national assessment" in t or " ana" in t:
        return "ANA (Grades 1-6 & 9)"
    return "Other"


def _year(text):
    m = re.search(r"\b(20\d{2})\b", text)
    return m.group(1) if m else "Unknown Year"


def scrape_index(session, grade_filters, year_filters, emit):
    # grade_filters / year_filters are lists; empty list = no filter = all
    emit("info", "Fetching index page…")
    r = _get(session, INDEX_URL, emit)
    if r is None:
        emit("error", "Could not fetch index page.")
        return []

    soup = BeautifulSoup(r.text, "lxml")
    sessions = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
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

        year  = _year(link_text) if _year(link_text) != "Unknown Year" else _year(href)
        grade = _grade(link_text + " " + href)
        title = _sanitise(link_text)

        if grade_filters and not any(gf in grade for gf in grade_filters):
            continue
        if year_filters and year not in year_filters:
            continue
        if any(s["url"] == full_url for s in sessions):
            continue

        sessions.append({"url": full_url, "title": title, "grade": grade, "year": year})

    emit("info", f"Found <strong>{len(sessions)}</strong> exam session(s) matching filters.")
    return sessions


def scrape_session(session, es, emit):
    emit("scan", f"Scanning: {es['title']}")
    r = _get(session, es["url"], emit)
    if r is None:
        return []

    soup  = BeautifulSoup(r.text, "lxml")
    files = []
    seen  = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        ext  = Path(urlparse(href).path).suffix.lower()
        if ext not in (".pdf", ".zip", ".docx"):
            continue
        file_url = urljoin(es["url"], href)
        if file_url in seen:
            continue
        seen.add(file_url)

        raw  = a.get_text(separator=" ", strip=True) or Path(urlparse(href).path).stem
        name = _sanitise(raw)
        if not name.lower().endswith(ext):
            name += ext
        files.append({"url": file_url, "filename": name, "exam_session": es})

    return files


def download_file(session, fi, root, dry_run, emit):
    es       = fi["exam_session"]
    dest_dir = Path(root) / _sanitise(es["grade"]) / es["year"] / _sanitise(es["title"])
    dest     = dest_dir / fi["filename"]

    if dry_run:
        emit("dryrun", f"{dest}")
        return "dryrun"

    if dest.exists():
        return "skipped"

    dest_dir.mkdir(parents=True, exist_ok=True)
    r = _get(session, fi["url"], emit)
    if r is None:
        emit("error", f"Failed: {fi['url']}")
        return "failed"

    dest.write_bytes(r.content)
    emit("download", f"{fi['filename']}")
    return "downloaded"


# ─── Flask app ────────────────────────────────────────────────────────────────

app = Flask(__name__)

# Active job state
_job_lock   = threading.Lock()
_job_queue  = None   # queue.Queue for SSE messages
_job_thread = None
_job_active = False


def run_job(grade_filters, year_filters, output_dir, dry_run, threads):
    global _job_active
    q = _job_queue

    def emit(kind, msg):
        q.put({"kind": kind, "msg": msg})

    try:
        http     = _session()
        sessions = scrape_index(http, grade_filters, year_filters, emit)
        if not sessions:
            emit("done", json.dumps({"dl": 0, "skip": 0, "fail": 0, "dry": 0}))
            return

        all_files = []
        for es in sessions:
            files = scrape_session(http, es, emit)
            all_files.extend(files)
            emit("progress", json.dumps({"scanned": len(all_files)}))

        emit("info", f"Total files found: <strong>{len(all_files)}</strong>")

        counts = {"downloaded": 0, "skipped": 0, "failed": 0, "dryrun": 0}

        import concurrent.futures
        def _task(fi):
            return download_file(http, fi, output_dir, dry_run, emit)

        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
            futures = {pool.submit(_task, fi): fi for fi in all_files}
            done    = 0
            total   = len(all_files)
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                counts[result] = counts.get(result, 0) + 1
                done += 1
                emit("progress", json.dumps({"done": done, "total": total}))

        emit("done", json.dumps({
            "dl":   counts["downloaded"],
            "skip": counts["skipped"],
            "fail": counts["failed"],
            "dry":  counts["dryrun"],
        }))

    except Exception as e:
        emit("error", f"Unexpected error: {e}")
        emit("done", json.dumps({"dl": 0, "skip": 0, "fail": 0, "dry": 0}))
    finally:
        _job_active = False


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/start", methods=["POST"])
def start():
    global _job_queue, _job_thread, _job_active

    with _job_lock:
        if _job_active:
            return jsonify({"error": "A job is already running."}), 409

        data         = request.json
        grades_raw   = data.get("grades", [])   # list of grade numbers e.g. ["12","11"]
        years_raw    = data.get("years",  [])   # list of year strings  e.g. ["2024","2023"]
        output_dir   = data.get("output_dir", "downloads").strip() or "downloads"
        dry_run      = bool(data.get("dry_run", False))
        threads      = max(1, min(10, int(data.get("threads", 3))))

        grade_filters = [f"Grade {g}" for g in grades_raw] if grades_raw else []
        year_filters  = [str(y) for y in years_raw] if years_raw else []

        _job_queue  = queue.Queue()
        _job_active = True
        _job_thread = threading.Thread(
            target=run_job,
            args=(grade_filters, year_filters, output_dir, dry_run, threads),
            daemon=True,
        )
        _job_thread.start()

    return jsonify({"status": "started"})


@app.route("/stream")
def stream():
    def generate():
        while True:
            with _job_lock:
                q = _job_queue
            if q is None:
                time.sleep(0.1)
                continue
            try:
                msg = q.get(timeout=1.0)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg["kind"] == "done":
                    break
            except queue.Empty:
                yield "data: {\"kind\":\"ping\"}\n\n"
                if not _job_active:
                    break

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/stop", methods=["POST"])
def stop():
    global _job_active
    _job_active = False
    return jsonify({"status": "stopping"})


# ─── HTML Template ────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>ECExams Scraper</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;600;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:       #0d0f14;
    --surface:  #14181f;
    --border:   #1e2530;
    --accent:   #f5a623;
    --accent2:  #e8483a;
    --green:    #3ecf8e;
    --muted:    #4a5568;
    --text:     #e2e8f0;
    --subtext:  #718096;
    --mono:     'Space Mono', monospace;
    --sans:     'Syne', sans-serif;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    min-height: 100vh;
    display: grid;
    grid-template-rows: auto 1fr;
    overflow: hidden;
  }

  /* ── Header ── */
  header {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 18px 32px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  .logo-mark {
    width: 36px; height: 36px;
    background: var(--accent);
    clip-path: polygon(50% 0%, 100% 25%, 100% 75%, 50% 100%, 0% 75%, 0% 25%);
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 14px; color: #000;
    flex-shrink: 0;
  }
  header h1 {
    font-family: var(--sans);
    font-weight: 800;
    font-size: 20px;
    letter-spacing: -0.5px;
  }
  header h1 span { color: var(--accent); }
  .header-sub {
    margin-left: auto;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 0.05em;
  }

  /* ── Layout ── */
  .workspace {
    display: grid;
    grid-template-columns: 340px 1fr;
    height: calc(100vh - 67px);
    overflow: hidden;
  }

  /* ── Left Panel ── */
  .panel-left {
    background: var(--surface);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow-y: auto;
  }

  .panel-section {
    padding: 24px;
    border-bottom: 1px solid var(--border);
  }
  .panel-section:last-child { border-bottom: none; flex: 1; }

  .section-label {
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 16px;
  }

  .field { margin-bottom: 14px; }
  .field label {
    display: block;
    font-size: 12px;
    color: var(--subtext);
    margin-bottom: 6px;
    font-family: var(--mono);
  }
  .field input, .field select {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 9px 12px;
    border-radius: 6px;
    font-family: var(--mono);
    font-size: 13px;
    outline: none;
    transition: border-color 0.15s;
  }
  .field input:focus, .field select:focus { border-color: var(--accent); }
  .field input::placeholder { color: var(--muted); }

  .field select option { background: var(--surface); }

  /* Checkbox group */
  .cb-group {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    max-height: 160px;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: var(--border) transparent;
  }
  .cb-group::-webkit-scrollbar { width: 4px; }
  .cb-group::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

  .cb-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 7px 12px;
    cursor: pointer;
    transition: background 0.1s;
    border-bottom: 1px solid rgba(255,255,255,0.03);
  }
  .cb-item:last-child { border-bottom: none; }
  .cb-item:hover { background: rgba(255,255,255,0.04); }
  .cb-item input[type=checkbox] { display: none; }
  .cb-box {
    width: 15px; height: 15px;
    border: 1.5px solid var(--muted);
    border-radius: 3px;
    flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    transition: all 0.15s;
  }
  .cb-item input:checked ~ .cb-box {
    background: var(--accent);
    border-color: var(--accent);
  }
  .cb-item input:checked ~ .cb-box::after {
    content: "";
    display: block;
    width: 4px; height: 7px;
    border: 2px solid #000;
    border-top: none; border-left: none;
    transform: rotate(45deg) translateY(-1px);
  }
  .cb-label {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--subtext);
    user-select: none;
  }
  .cb-item input:checked ~ .cb-box ~ .cb-label { color: var(--text); }

  .cb-controls {
    display: flex;
    gap: 8px;
    margin-bottom: 6px;
  }
  .cb-ctrl-btn {
    background: none;
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 3px 9px;
    border-radius: 4px;
    font-family: var(--mono);
    font-size: 10px;
    cursor: pointer;
    transition: all 0.15s;
    letter-spacing: 0.05em;
  }
  .cb-ctrl-btn:hover { border-color: var(--accent); color: var(--accent); }

  /* Range slider */
  .range-row {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  input[type=range] {
    flex: 1;
    -webkit-appearance: none;
    height: 4px;
    background: var(--border);
    border-radius: 2px;
    border: none;
    padding: 0;
  }
  input[type=range]::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 16px; height: 16px;
    background: var(--accent);
    border-radius: 50%;
    cursor: pointer;
  }
  .range-val {
    font-family: var(--mono);
    font-size: 13px;
    color: var(--accent);
    min-width: 18px;
    text-align: center;
  }

  /* Toggle */
  .toggle-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 0;
  }
  .toggle-row span {
    font-size: 13px;
    color: var(--subtext);
    font-family: var(--mono);
  }
  .toggle {
    position: relative;
    width: 40px; height: 22px;
  }
  .toggle input { opacity: 0; width: 0; height: 0; }
  .slider-toggle {
    position: absolute; inset: 0;
    background: var(--border);
    border-radius: 22px;
    cursor: pointer;
    transition: background 0.2s;
  }
  .slider-toggle::before {
    content: "";
    position: absolute;
    width: 16px; height: 16px;
    left: 3px; top: 3px;
    background: #fff;
    border-radius: 50%;
    transition: transform 0.2s;
  }
  .toggle input:checked + .slider-toggle { background: var(--accent); }
  .toggle input:checked + .slider-toggle::before { transform: translateX(18px); }

  /* Buttons */
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 11px 20px;
    border-radius: 7px;
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 700;
    cursor: pointer;
    border: none;
    transition: all 0.15s;
    letter-spacing: 0.03em;
  }
  .btn-primary {
    background: var(--accent);
    color: #000;
    width: 100%;
  }
  .btn-primary:hover { background: #ffb740; }
  .btn-primary:disabled { background: var(--muted); color: var(--border); cursor: not-allowed; }
  .btn-danger {
    background: transparent;
    color: var(--accent2);
    border: 1px solid var(--accent2);
    width: 100%;
    margin-top: 8px;
  }
  .btn-danger:hover { background: rgba(232,72,58,0.1); }
  .btn-danger:disabled { opacity: 0.3; cursor: not-allowed; }

  /* Stats */
  .stat-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    margin-top: 16px;
  }
  .stat-card {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
  }
  .stat-card .val {
    font-family: var(--mono);
    font-size: 22px;
    font-weight: 700;
    color: var(--text);
  }
  .stat-card .lbl {
    font-size: 10px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-top: 2px;
    font-family: var(--mono);
  }
  .stat-card.green .val { color: var(--green); }
  .stat-card.amber .val { color: var(--accent); }
  .stat-card.red   .val { color: var(--accent2); }

  /* Progress bar */
  .progress-wrap {
    margin-top: 16px;
    background: var(--border);
    border-radius: 4px;
    height: 6px;
    overflow: hidden;
  }
  .progress-bar {
    height: 100%;
    background: linear-gradient(90deg, var(--accent), var(--green));
    border-radius: 4px;
    width: 0%;
    transition: width 0.3s ease;
  }
  .progress-label {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    margin-top: 6px;
    text-align: right;
  }

  /* Status dot */
  .status-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 0 0;
  }
  .dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--muted);
    flex-shrink: 0;
  }
  .dot.active { background: var(--green); animation: pulse 1.2s infinite; }
  .dot.done   { background: var(--green); }
  .dot.error  { background: var(--accent2); }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.3; }
  }
  .status-text { font-family: var(--mono); font-size: 12px; color: var(--subtext); }

  /* ── Right Panel (log) ── */
  .panel-right {
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg);
  }

  .log-header {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 14px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    flex-shrink: 0;
  }
  .log-header h2 {
    font-family: var(--mono);
    font-size: 12px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .log-header .clear-btn {
    margin-left: auto;
    background: none;
    border: 1px solid var(--border);
    color: var(--subtext);
    padding: 4px 12px;
    border-radius: 4px;
    font-family: var(--mono);
    font-size: 11px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .log-header .clear-btn:hover { border-color: var(--muted); color: var(--text); }

  .log-body {
    flex: 1;
    overflow-y: auto;
    padding: 16px 24px;
    font-family: var(--mono);
    font-size: 12.5px;
    line-height: 1.7;
    scrollbar-width: thin;
    scrollbar-color: var(--border) transparent;
  }
  .log-body::-webkit-scrollbar { width: 5px; }
  .log-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  .log-entry {
    display: flex;
    gap: 12px;
    padding: 3px 0;
    border-bottom: 1px solid rgba(255,255,255,0.02);
    animation: fadeIn 0.15s ease;
  }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: none; } }

  .log-ts   { color: var(--muted); flex-shrink: 0; width: 70px; }
  .log-kind { flex-shrink: 0; width: 70px; }
  .log-msg  { color: var(--text); word-break: break-all; flex: 1; }

  .kind-info     .log-kind { color: #60a5fa; }
  .kind-scan     .log-kind { color: #a78bfa; }
  .kind-download .log-kind { color: var(--green); }
  .kind-warn     .log-kind { color: var(--accent); }
  .kind-error    .log-kind { color: var(--accent2); }
  .kind-dryrun   .log-kind { color: #94a3b8; }
  .kind-done     .log-kind { color: var(--green); font-weight: 700; }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    gap: 12px;
    color: var(--muted);
    font-family: var(--mono);
    font-size: 13px;
  }
  .empty-icon { font-size: 40px; opacity: 0.3; }

  /* Responsive */
  @media (max-width: 768px) {
    .workspace { grid-template-columns: 1fr; grid-template-rows: auto 1fr; }
    .panel-left { height: auto; overflow: visible; }
    body { overflow: auto; }
  }
</style>
</head>
<body>

<header>
  <div class="logo-mark">EC</div>
  <h1>ECExams <span>Scraper</span></h1>
  <span class="header-sub">ecexams.co.za · PDF downloader</span>
</header>

<div class="workspace">

  <!-- ── Left Panel ────────────────────────────────── -->
  <aside class="panel-left">

    <div class="panel-section">
      <div class="section-label">Filters</div>

      <div class="field">
        <label>Grade</label>
        <div class="cb-controls">
          <button class="cb-ctrl-btn" onclick="setAll('gradeGroup', true)">All</button>
          <button class="cb-ctrl-btn" onclick="setAll('gradeGroup', false)">None</button>
        </div>
        <div class="cb-group" id="gradeGroup">
          <label class="cb-item"><input type="checkbox" value="12"/><span class="cb-box"></span><span class="cb-label">Grade 12</span></label>
          <label class="cb-item"><input type="checkbox" value="11"/><span class="cb-box"></span><span class="cb-label">Grade 11</span></label>
          <label class="cb-item"><input type="checkbox" value="10"/><span class="cb-box"></span><span class="cb-label">Grade 10</span></label>
          <label class="cb-item"><input type="checkbox" value="9"/><span class="cb-box"></span><span class="cb-label">Grade 9</span></label>
          <label class="cb-item"><input type="checkbox" value="7"/><span class="cb-box"></span><span class="cb-label">Grade 7</span></label>
          <label class="cb-item"><input type="checkbox" value="6"/><span class="cb-box"></span><span class="cb-label">Grade 6</span></label>
          <label class="cb-item"><input type="checkbox" value="3"/><span class="cb-box"></span><span class="cb-label">Grade 3</span></label>
        </div>
      </div>

      <div class="field">
        <label>Year</label>
        <div class="cb-controls">
          <button class="cb-ctrl-btn" onclick="setAll('yearGroup', true)">All</button>
          <button class="cb-ctrl-btn" onclick="setAll('yearGroup', false)">None</button>
        </div>
        <div class="cb-group" id="yearGroup">
          <!-- populated by JS -->
        </div>
      </div>

      <div class="field">
        <label>Output folder</label>
        <input id="outputDir" type="text" value="downloads" placeholder="downloads"/>
      </div>

      <div class="field">
        <label>Parallel threads</label>
        <div class="range-row">
          <input id="threads" type="range" min="1" max="10" value="3"
                 oninput="document.getElementById('threadsVal').textContent=this.value"/>
          <span class="range-val" id="threadsVal">3</span>
        </div>
      </div>

      <div class="toggle-row">
        <span>Dry run (preview only)</span>
        <label class="toggle">
          <input id="dryRun" type="checkbox"/>
          <span class="slider-toggle"></span>
        </label>
      </div>
    </div>

    <div class="panel-section">
      <div class="section-label">Actions</div>
      <button id="startBtn" class="btn btn-primary" onclick="startJob()">
        ▶ &nbsp;Start Download
      </button>
      <button id="stopBtn" class="btn btn-danger" onclick="stopJob()" disabled>
        ■ &nbsp;Stop
      </button>

      <div class="status-row">
        <div class="dot" id="statusDot"></div>
        <span class="status-text" id="statusText">Ready</span>
      </div>
    </div>

    <div class="panel-section">
      <div class="section-label">Progress</div>

      <div class="stat-grid">
        <div class="stat-card green">
          <div class="val" id="statDl">—</div>
          <div class="lbl">Downloaded</div>
        </div>
        <div class="stat-card amber">
          <div class="val" id="statSkip">—</div>
          <div class="lbl">Skipped</div>
        </div>
        <div class="stat-card red">
          <div class="val" id="statFail">—</div>
          <div class="lbl">Failed</div>
        </div>
        <div class="stat-card">
          <div class="val" id="statTotal">—</div>
          <div class="lbl">Total</div>
        </div>
      </div>

      <div class="progress-wrap">
        <div class="progress-bar" id="progressBar"></div>
      </div>
      <div class="progress-label" id="progressLabel"></div>
    </div>

  </aside>

  <!-- ── Right Panel (log) ────────────────────────── -->
  <main class="panel-right">
    <div class="log-header">
      <h2>Live Output</h2>
      <button class="clear-btn" onclick="clearLog()">Clear</button>
    </div>
    <div class="log-body" id="logBody">
      <div class="empty-state" id="emptyState">
        <div class="empty-icon">📄</div>
        <span>Configure filters and press Start Download</span>
      </div>
    </div>
  </main>

</div>

<script>
  // ── Populate year checkboxes ──────────────────────────────────
  const yearGroup = document.getElementById('yearGroup');
  const currentYear = new Date().getFullYear();
  for (let y = currentYear; y >= 2008; y--) {
    const lbl = document.createElement('label');
    lbl.className = 'cb-item';
    lbl.innerHTML = `<input type="checkbox" value="${y}"/><span class="cb-box"></span><span class="cb-label">${y}</span>`;
    yearGroup.appendChild(lbl);
  }

  // ── Checkbox helpers ─────────────────────────────────────────
  function setAll(groupId, checked) {
    document.querySelectorAll(`#${groupId} input[type=checkbox]`)
      .forEach(cb => cb.checked = checked);
  }

  function getChecked(groupId) {
    return [...document.querySelectorAll(`#${groupId} input[type=checkbox]:checked`)]
      .map(cb => cb.value);
  }

  // ── State ───────────────────────────────────────────
  let es        = null;   // EventSource
  let running   = false;
  let totalFiles = 0;
  let doneFiles  = 0;

  function ts() {
    const d = new Date();
    return d.toTimeString().slice(0,8);
  }

  function appendLog(kind, msg) {
    const body = document.getElementById('logBody');
    const empty = document.getElementById('emptyState');
    if (empty) empty.remove();

    const row = document.createElement('div');
    row.className = `log-entry kind-${kind}`;

    const labels = {
      info: 'INFO', scan: 'SCAN', download: 'DONE',
      warn: 'WARN', error: 'ERR ', dryrun: 'DRY ',
      done: 'DONE', progress: null,
    };
    if (labels[kind] === null) return; // suppress progress lines in log

    row.innerHTML = `
      <span class="log-ts">${ts()}</span>
      <span class="log-kind">${labels[kind] || kind}</span>
      <span class="log-msg">${msg}</span>
    `;
    body.appendChild(row);
    body.scrollTop = body.scrollHeight;
  }

  function clearLog() {
    const body = document.getElementById('logBody');
    body.innerHTML = '<div class="empty-state" id="emptyState"><div class="empty-icon">📄</div><span>Log cleared. Ready for a new run.</span></div>';
  }

  function setStatus(state, text) {
    const dot  = document.getElementById('statusDot');
    const stxt = document.getElementById('statusText');
    dot.className  = `dot ${state}`;
    stxt.textContent = text;
  }

  function setButtons(isRunning) {
    running = isRunning;
    document.getElementById('startBtn').disabled = isRunning;
    document.getElementById('stopBtn').disabled  = !isRunning;
  }

  function updateProgress(done, total) {
    doneFiles  = done  !== undefined ? done  : doneFiles;
    totalFiles = total !== undefined ? total : totalFiles;
    const pct  = totalFiles > 0 ? Math.round((doneFiles / totalFiles) * 100) : 0;
    document.getElementById('progressBar').style.width  = pct + '%';
    document.getElementById('progressLabel').textContent =
      totalFiles > 0 ? `${doneFiles} / ${totalFiles} files  (${pct}%)` : '';
    document.getElementById('statTotal').textContent = totalFiles || '—';
  }

  // ── Start ───────────────────────────────────────────
  function startJob() {
    if (running) return;
    clearLog();
    totalFiles = 0; doneFiles = 0;
    updateProgress(0, 0);
    ['statDl','statSkip','statFail'].forEach(id => document.getElementById(id).textContent = '—');

    const payload = {
      grades:     getChecked('gradeGroup'),
      years:      getChecked('yearGroup'),
      output_dir: document.getElementById('outputDir').value,
      dry_run:    document.getElementById('dryRun').checked,
      threads:    parseInt(document.getElementById('threads').value),
    };

    fetch('/start', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload),
    }).then(r => r.json()).then(d => {
      if (d.error) { appendLog('error', d.error); return; }
      setButtons(true);
      setStatus('active', 'Running…');
      listenStream();
    }).catch(e => appendLog('error', String(e)));
  }

  // ── Stop ────────────────────────────────────────────
  function stopJob() {
    fetch('/stop', {method:'POST'});
    setStatus('', 'Stopping…');
  }

  // ── Stream ──────────────────────────────────────────
  function listenStream() {
    if (es) { es.close(); }
    es = new EventSource('/stream');

    es.onmessage = (e) => {
      const data = JSON.parse(e.data);
      const { kind, msg } = data;

      if (kind === 'ping') return;

      if (kind === 'progress') {
        const p = JSON.parse(msg);
        updateProgress(p.done, p.total !== undefined ? p.total : totalFiles);
        return;
      }

      if (kind === 'done') {
        const stats = JSON.parse(msg);
        document.getElementById('statDl').textContent   = stats.dl;
        document.getElementById('statSkip').textContent = stats.skip;
        document.getElementById('statFail').textContent = stats.fail;
        setButtons(false);
        setStatus('done', 'Completed');
        appendLog('done', `Finished — ${stats.dl} downloaded, ${stats.skip} skipped, ${stats.fail} failed`);
        es.close();
        return;
      }

      appendLog(kind, msg);
    };

    es.onerror = () => {
      setButtons(false);
      setStatus('error', 'Connection lost');
    };
  }
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("\n  ECExams Scraper  →  http://localhost:5000\n")
    app.run(debug=False, threaded=True, host="0.0.0.0", port=5000)
