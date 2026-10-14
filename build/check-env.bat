@echo off
chcp 65001 >nul
REM ============================================================================
REM  Environment health check for local RustDesk builds.
REM  Tells you exactly which components are missing before a 40-minute
REM  build fails halfway through.
REM ============================================================================
python "%~dp0check-env.py"
echo.
pause
