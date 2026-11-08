@echo off
cd /d "%~dp0"
if not exist .venv (py -3 -m venv .venv 2>nul || python -m venv .venv)
.venv\Scripts\python -m pip install -q -r requirements.txt
.venv\Scripts\python run.py %*
pause
