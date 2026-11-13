@echo off
setlocal
cd /d "%~dp0"
call RUNME.bat --config config/brainco.toml --check-device %*
exit /b %errorlevel%
