@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto :failed
)
".venv\Scripts\python.exe" -m pip install -r requirements-demo.txt
if errorlevel 1 goto :failed
echo Demo: http://127.0.0.1:8082/?demo=result
".venv\Scripts\python.exe" demo.py
if errorlevel 1 goto :failed
exit /b 0
:failed
echo Could not start the demo. Install Python 3.11+ and check the error above.
pause
exit /b 1
