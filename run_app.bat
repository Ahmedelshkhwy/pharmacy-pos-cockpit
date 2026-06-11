@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found. Create it first with:
  echo   python -m venv .venv
  echo   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)

start "Pharmacy App" ".venv\Scripts\python.exe" app.py
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:5000

endlocal
