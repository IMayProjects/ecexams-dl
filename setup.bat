@echo off
REM setup.bat – Sets up a virtual environment and installs all dependencies
REM Works on Windows. Double-click to run, or run from Command Prompt.

echo.
echo +--------------------------------------+
echo ^|   ECExams Scraper - Setup            ^|
echo +--------------------------------------+
echo.

REM ── 1. Find Python ─────────────────────────────────────────────────────────
python --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo [ERROR] Python not found. Please install it from https://www.python.org/downloads/
    echo         Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

FOR /F "tokens=*" %%i IN ('python --version') DO echo [OK] Found %%i

REM ── 2. Create virtual environment ──────────────────────────────────────────
IF EXIST venv (
    echo [OK] Virtual environment already exists at .\venv
) ELSE (
    echo [-^>] Creating virtual environment...
    python -m venv venv
    echo [OK] Virtual environment created
)

REM ── 3. Install dependencies ────────────────────────────────────────────────
echo [-^>] Activating virtual environment...
call venv\Scripts\activate.bat

echo [-^>] Upgrading pip...
python -m pip install --upgrade pip --quiet

echo [-^>] Installing dependencies...
pip install -r requirements.txt

echo.
echo +----------------------------------------------------------+
echo ^| Setup complete!                                          ^|
echo ^|                                                          ^|
echo ^| To start the web UI:                                     ^|
echo ^|   venv\Scripts\activate                                  ^|
echo ^|   python app.py                                          ^|
echo ^|   then open http://localhost:5000                        ^|
echo ^|                                                          ^|
echo ^| Or use the CLI scraper:                                  ^|
echo ^|   python ecexams_scraper.py --help                       ^|
echo +----------------------------------------------------------+
echo.
pause
