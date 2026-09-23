@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist ".venv\Scripts\python.exe" (
  echo First run: installing Walter. This takes a few minutes...
  %PY% -m venv .venv || (echo Install Python 3.11+ from python.org and tick "Add to PATH". & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (pause & exit /b 1)
  ".venv\Scripts\python.exe" -m playwright install chromium || (pause & exit /b 1)
)
".venv\Scripts\python.exe" -m heisenbot ui
pause
