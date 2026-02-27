# ECExams DL

A web scraper for [ecexams.co.za](https://www.ecexams.co.za/ExaminationPapers.htm) that downloads South African exam
papers and memos and organises them into a clean folder hierarchy. Ships with both a **browser-based UI** and a *
*command-line interface**.

## Useage

#### A.

This software is a tool for accessing publicly available educational materials hosted
on [ecexams.co.za](https://www.ecexams.co.za). The exam papers and memos downloaded by this tool are the intellectual
property of the Department of Basic Education of South Africa and the Eastern Cape Department of Education. They are
made freely available for educational purposes.

#### B.

- This tool is intended for **personal and educational use only**. You are responsible for ensuring your use of
  downloaded materials complies with applicable copyright law and the terms of the source website.
- Please respect the site's content and do not hammer the server
  with aggressive settings. The scraper introduces a deliberate 0.4 s delay between requests. Please do not reduce this
  significantly.
- If a session page returns no files, it is likely temporarily unavailable - re-running will retry it.

---

## Features

- **Web UI** - filter by grade and year using checkboxes, watch progress stream in real time, start/stop jobs from the
  browser
- **CLI** - scriptable, supports `--dry-run`, `--grade`, `--year`, `--threads`, and `--output-dir` flags
- **Organised output** - files are saved as `downloads/<Grade>/<Year>/<Session>/<filename>.pdf`
- **Resumable** - already-downloaded files are skipped, so re-running is always safe
- **Polite** - 0.4 s delay between requests, exponential back-off on retries, configurable thread count
- **Parallel downloads** - up to 10 concurrent threads via a slider in the UI or `--threads` on the CLI

---

## Folder structure after download

```
downloads/
├── Grade 12/
│   ├── 2024/
│   │   ├── November NSC Grade 12 Examinations/
│   │   │   ├── Mathematics P1.pdf
│   │   │   ├── Mathematics P1 Memo.pdf
│   │   │   └── ...
│   │   └── MayJune Grade 12 NSC DBE Examinations/
│   │       └── ...
│   └── 2023/
│       └── ...
├── Grade 11/
│   └── ...
└── Grade 9 (GEC)/
    └── ...
```

---

## Quick start

### 1. Prerequisites

- Python 3.8 or higher - download from [python.org](https://www.python.org/downloads/)
  - Windows: tick **"Add Python to PATH"** during installation

### 2. Clone the repo

```bash
git clone https://github.com/IMayProject/ecexams-dl.git
cd ecexams-scraper
```

### 3. Run the setup script

**macOS / Linux**

```bash
bash setup.sh
source venv/bin/activate
```

**Windows**

```cmd
setup.bat
venv\Scripts\activate.bat
```

The script creates an isolated virtual environment and installs all dependencies automatically.

### 4. Start the web UI

run the following command in the terminal:

```bash
python app.py
```

> [!TIP]
> On Windows, you can **double click** the `run.bat` script in the root directory.
>

Open **http://localhost:5000** in your browser.

### 5. Or use the CLI directly

#### Preview what would be downloaded without saving anything

```bash
python ecexams_scraper.py --dry-run
```

#### Download all Grade 12 papers from 2024

```bash
python ecexams_scraper.py --grade 12 --year 2024
```

#### Download everything with 5 parallel threads

```bash
python ecexams_scraper.py --threads 5
```

#### Save to a custom folder

```bash
python ecexams_scraper.py --grade 12 --output-dir ~/Desktop/ExamPapers
```

---

## File overview

| File                 | Purpose                                                       |
|----------------------|---------------------------------------------------------------|
| `app.py`             | Flask web application - UI + scraper core bundled in one file |
| `ecexams_scraper.py` | Standalone CLI scraper                                        |
| `requirements.txt`   | Python dependencies                                           |
| `setup.sh`           | Automated setup for macOS / Linux                             |
| `setup.bat`          | Automated setup for Windows                                   |

---

## Dependencies

| Package          | Version | Purpose                      |
|------------------|---------|------------------------------|
| `flask`          | ≥ 3.0   | Web server and SSE streaming |
| `requests`       | ≥ 2.31  | HTTP fetching                |
| `beautifulsoup4` | ≥ 4.12  | HTML parsing                 |
| `lxml`           | ≥ 5.0   | Fast HTML parser backend     |

---

## CLI reference

```
usage: ecexams_scraper.py [-h] [--grade GRADE] [--year YEAR]
                          [--dry-run] [--threads THREADS]
                          [--output-dir OUTPUT_DIR]

options:
  --grade       Filter by grade number, e.g. 12
  --year        Filter by year, e.g. 2024
  --dry-run     List files without downloading anything
  --threads     Parallel download threads (default: 3, max: 10)
  --output-dir  Root download folder (default: downloads/)
```

---

## License

MIT
