@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
set "MAX_RESTARTS_PER_MINUTE=5"
set "RESTART_DELAY=5"

if not exist "%PY%" (
    echo Virtual environment not found at .venv. See README.md for setup.
    exit /b 1
)

set "RESTARTS=0"
call :now WINDOW_START

:loop
"%PY%" main.py
set "EXITCODE=%ERRORLEVEL%"

if "%EXITCODE%"=="0" (
    echo Bot stopped cleanly.
    exit /b 0
)
if "%EXITCODE%"=="2" (
    echo Configuration or login error. Fix .env and start again. Not restarting.
    exit /b 2
)

call :now NOW
set /a ELAPSED=NOW-WINDOW_START
if !ELAPSED! lss 0 set /a ELAPSED=60
if !ELAPSED! geq 60 (
    set "RESTARTS=0"
    set "WINDOW_START=!NOW!"
)
set /a RESTARTS+=1
if !RESTARTS! gtr %MAX_RESTARTS_PER_MINUTE% (
    echo Exceeded %MAX_RESTARTS_PER_MINUTE% restarts in one minute. Giving up. Check logs\bot.log.
    exit /b 1
)

echo Bot exited with code !EXITCODE!. Restart !RESTARTS! of %MAX_RESTARTS_PER_MINUTE% allowed this minute. Waiting %RESTART_DELAY% seconds...
timeout /t %RESTART_DELAY% /nobreak >nul
goto loop

:now
rem Seconds since midnight, written to the variable named by the first argument.
for /f "tokens=1-3 delims=:., " %%a in ("%TIME: =0%") do set /a %1=(1%%a-100)*3600+(1%%b-100)*60+(1%%c-100)
exit /b 0
