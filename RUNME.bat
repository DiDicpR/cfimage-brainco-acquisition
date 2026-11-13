@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py %*
) else (
    echo Run uv sync --locked first. A project .venv is required.
    pause
    exit /b 1
)
set "result=%errorlevel%"
if not "%result%"=="0" echo Experiment exited with code %result%.
pause
exit /b %result%
