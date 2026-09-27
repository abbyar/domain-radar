@echo off
setlocal
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe run_all.py %*
) else (
    python run_all.py %*
)
pause
