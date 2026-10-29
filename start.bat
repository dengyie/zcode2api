@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

rem Proxy fallback (parity with start.sh / e3222ef): Clash system proxy may
rem inject a socks:// ALL_PROXY, which httpx cannot use -> every upstream
rem request 500s. Strip it only when the scheme is not http(s). On Windows
rem var names are case-insensitive, so ALL_PROXY also covers all_proxy.
rem Delayed expansion is required: %VAR:~0,7% of an undefined VAR collapses
rem at parse time and breaks the if statement; !VAR! expands safely at run.
rem Keep console output ASCII-only: Chinese in bat echo mojibakes badly.
set "STRIP=0"
if defined ALL_PROXY (
    set "STRIP=1"
    if /i "!ALL_PROXY:~0,7!"=="http://" set "STRIP=0"
    if /i "!ALL_PROXY:~0,8!"=="https://" set "STRIP=0"
)
if "!STRIP!"=="1" (
    echo [i] Stripped ALL_PROXY=!ALL_PROXY! - httpx does not support this proxy scheme
    set "ALL_PROXY="
)

echo Starting zcode-hub on http://127.0.0.1:3000 ...
".venv\Scripts\python.exe" cli.py serve
pause
