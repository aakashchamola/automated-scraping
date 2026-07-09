@echo off
REM One-command start (Windows): venv + deps + setup wizard (first run) + supervised monitor.
cd /d "%~dp0"

if not exist .venv (
    echo Creating Python environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt

python -c "import json,sys; sys.exit(0 if json.load(open('config.json'))['alerts']['ntfy']['topic'].strip() else 1)"
if errorlevel 1 python setup_wizard.py

python run_forever.py %*
