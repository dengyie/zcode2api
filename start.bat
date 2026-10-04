@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

rem =====================================================================
rem  zcode-hub Windows launcher -- feature parity with start.sh (Linux)
rem
rem  Usage:
rem    start.bat          Start (auto-stops a running instance first)
rem    start.bat stop     Stop
rem
rem  Notes:
rem    - Runs the server hidden in background; logs: serve.log + serve.err.log
rem    - Health check via GET /meta after start
rem    - Console messages are ASCII on purpose (avoids codepage mojibake)
rem =====================================================================

rem -- Edit as needed ----------------------------------------------------
rem Upstream proxy (Clash mixed port); leave empty "" for direct connection
set "PROXY=http://127.0.0.1:7890"
rem ----------------------------------------------------------------------

set "PORT=3000"
if exist .env for /f "tokens=1,* delims==" %%a in ('findstr /b /c:"ZCODE_PORT=" .env') do set "PORT=%%b"
if defined PORT set "PORT=%PORT: =%"
if not defined PORT set "PORT=3000"

rem Pause on exit only when launched by double-click (cmd /c), never in scripts
set "DBLCLK=0"
echo %cmdcmdline% | find /i "/c" >nul 2>&1 && set "DBLCLK=1"

if /i "%~1"=="stop" (
    call :stop_instance
    echo [OK] Stopped.
    if "%DBLCLK%"=="1" pause
    exit /b 0
)

rem -- 1. venv + Python dependencies -------------------------------------
if not exist ".venv\Scripts\python.exe" (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [!] python not found on PATH. Install Python 3.11+ first.
        goto :fail
    )
    echo [*] Creating venv (.venv) ...
    python -m venv .venv || goto :fail
)
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, httpx" >nul 2>&1
if errorlevel 1 (
    echo [*] Installing Python dependencies ...
    if defined PROXY (
        ".venv\Scripts\python.exe" -m pip install -q --proxy "%PROXY%" -r requirements.txt || goto :fail
    ) else (
        ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :fail
    )
)

rem -- 2. captcha_node (npm) dependencies --------------------------------
if not exist "captcha_node\node_modules" (
    echo [*] Installing captcha_node dependencies (npm install) ...
    pushd captcha_node
    call npm install --no-audit --no-fund
    if errorlevel 1 (
        popd
        goto :fail
    )
    popd
)

rem -- 3. Stop old instance (restart semantics, clears admin lock too) ---
call :stop_instance

rem -- 4. Port must be free ----------------------------------------------
set "OCCUPY="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do set "OCCUPY=%%p"
if defined OCCUPY (
    echo [!] Port %PORT% still occupied by another process ^(PID %OCCUPY%^):
    netstat -ano | findstr /r /c:":%PORT% .*LISTENING"
    echo     Stop it manually first, e.g.:  taskkill /F /PID %OCCUPY%
    goto :fail
)

rem -- 5. Start hidden in background, capture real python PID ------------
echo [*] Starting zcode-hub (port=%PORT%) ...
if defined PROXY (
    set "HTTP_PROXY=%PROXY%/"
    set "HTTPS_PROXY=%PROXY%/"
)
powershell -NoProfile -Command "$d=(Get-Location).Path; $p = Start-Process -FilePath (Join-Path $d '.venv\Scripts\python.exe') -ArgumentList 'cli.py','serve' -WorkingDirectory $d -WindowStyle Hidden -RedirectStandardOutput (Join-Path $d 'serve.log') -RedirectStandardError (Join-Path $d 'serve.err.log') -PassThru; Write-Output $p.Id" > "%TEMP%\zcode_hub_pid.txt" 2>nul
set "NEWPID="
set /p NEWPID=<"%TEMP%\zcode_hub_pid.txt"
del "%TEMP%\zcode_hub_pid.txt" >nul 2>&1
if not defined NEWPID (
    echo [!] Failed to launch python process.
    goto :fail
)
(echo %NEWPID%)>"serve.pid"

rem -- 6. Health check (up to ~15s) --------------------------------------
set /a TRIES=0
:health_wait
ping -n 2 127.0.0.1 >nul
curl -sf -m 2 "http://127.0.0.1:%PORT%/meta" >nul 2>&1 && goto :started
set /a TRIES+=1
if %TRIES% lss 15 goto :health_wait

echo [!] Start FAILED. Last log lines:
echo --- serve.log ---
powershell -NoProfile -Command "if (Test-Path serve.log) { Get-Content serve.log -Tail 15 }"
echo --- serve.err.log ---
powershell -NoProfile -Command "if (Test-Path serve.err.log) { Get-Content serve.err.log -Tail 15 }"
goto :fail

:started
echo [OK] Started:   http://127.0.0.1:%PORT%
echo     Admin UI :  http://127.0.0.1:%PORT%/admin/login
echo     Logs     :  serve.log + serve.err.log
echo     Stop     :  start.bat stop      Restart: just run start.bat again
if "%DBLCLK%"=="1" pause
exit /b 0

:fail
echo [!] start.bat aborted.
if "%DBLCLK%"=="1" pause
exit /b 1

:stop_instance
set "OLDPID="
if exist serve.pid for /f %%p in (serve.pid) do set "OLDPID=%%p"
if not defined OLDPID goto :stop_cleanup
tasklist /FI "PID eq %OLDPID%" /NH 2>nul | find /i "python.exe" >nul 2>&1 || goto :stop_cleanup
echo [*] Stopping old instance (pid=%OLDPID%) ...
taskkill /PID %OLDPID% >nul 2>&1
set /a N=0
:stop_wait
tasklist /FI "PID eq %OLDPID%" /NH 2>nul | find /i "python.exe" >nul 2>&1 || goto :stop_cleanup
set /a N+=1
if %N% geq 10 goto :stop_force
ping -n 2 127.0.0.1 >nul
goto :stop_wait
:stop_force
taskkill /F /PID %OLDPID% >nul 2>&1
:stop_cleanup
if exist serve.pid del serve.pid >nul 2>&1
goto :eof
