@echo off
setlocal enabledelayedexpansion

net session >nul 2>&1
if not %errorLevel% == 0 (
    powershell -NoProfile -Command "Start-Process cmd -ArgumentList '/c \"%~f0\"' -Verb RunAs -WorkingDirectory '%~dp0'"
    exit /b
)

cd /d "%~dp0"
set "PROJECT_ROOT=%~dp0"
set "ENTRY_FILE=opentune_sync.py"

set "LOGDIR_TEMP=%TEMP%\opentune_sync_setup"
if not exist "%LOGDIR_TEMP%" mkdir "%LOGDIR_TEMP%"
for /f %%t in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "TS=%%t"
set "LOGFILE=%LOGDIR_TEMP%\setup_%TS%.log"

call :log "=== OpenTune Sync setup starting ==="
call :log "Project root: %PROJECT_ROOT%"

call :log "Checking Python (need >= 3.8)..."
set "PYCMD="

where python >nul 2>&1
if %errorLevel% == 0 (
    for /f %%v in ('python -c "import sys;print(1 if sys.version_info>=(3,8) else 0)" 2^>nul') do set "PYOK=%%v"
    if "!PYOK!"=="1" set "PYCMD=python"
)

if not defined PYCMD (
    where py >nul 2>&1
    if %errorLevel% == 0 (
        for /f %%v in ('py -c "import sys;print(1 if sys.version_info>=(3,8) else 0)" 2^>nul') do set "PYOK=%%v"
        if "!PYOK!"=="1" set "PYCMD=py"
    )
)

if not defined PYCMD (
    call :log "Python 3.8+ not found. Installing via winget..."
    where winget >nul 2>&1
    if not %errorLevel% == 0 (
        call :fail "winget not available. Install Python 3.8+ manually from python.org and re-run."
    )
    winget install -e --id Python.Python.3.12 --scope machine --silent --accept-package-agreements --accept-source-agreements
    if not %errorLevel% == 0 (
        call :fail "winget install of Python failed."
    )

    for /f "delims=" %%p in ('powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable(''Path'',''Machine'')+'';''+[Environment]::GetEnvironmentVariable(''Path'',''User'')"') do set "PATH=%%p"

    where python >nul 2>&1
    if %errorLevel% == 0 (
        set "PYCMD=python"
    ) else (
        call :fail "Python install did not complete correctly. Restart the PC and re-run this script."
    )
)

call :log "Using Python command: !PYCMD!"
for /f "delims=" %%v in ('!PYCMD! -c "import sys;print(sys.version.split()[0])"') do call :log "Python !%%v! OK."

call :log "Checking pip availability..."
!PYCMD! -m pip --version >nul 2>&1
if not %errorLevel% == 0 (
    call :fail "pip is not available for this Python interpreter."
)
call :log "pip OK."

call :log "Installing/verifying required libraries..."
call :progress 0 1
!PYCMD! -c "import flask" >nul 2>&1
if %errorLevel% == 0 (
    call :log "flask: already installed."
) else (
    call :log "flask: not found, installing..."
    !PYCMD! -m pip install flask >>"%LOGFILE%" 2>&1
    if not %errorLevel% == 0 (
        call :log "[ERROR] pip output:"
        type "%LOGFILE%" | findstr /i "error"
        call :fail "Failed to install flask."
    )
    call :log "flask: installed successfully."
)
call :progress 1 1

set "ENTRY_PATH="
if exist "%PROJECT_ROOT%%ENTRY_FILE%" (
    set "ENTRY_PATH=%PROJECT_ROOT%%ENTRY_FILE%"
) else (
    for %%f in ("%PROJECT_ROOT%*opentune_sync*.py") do (
        if not defined ENTRY_PATH set "ENTRY_PATH=%%f"
    )
)

if not defined ENTRY_PATH (
    call :fail "Could not find entry file '%ENTRY_FILE%' in %PROJECT_ROOT%."
)

call :log "Entry file located: !ENTRY_PATH!"
call :flushlog

call :log "Starting OpenTune Sync..."
!PYCMD! "!ENTRY_PATH!"
if not %errorLevel% == 0 (
    call :fail "OpenTune Sync exited with error code %errorLevel%."
)

call :flushlog
pause
exit /b 0

:log
set "MSG=%~1"
for /f "delims=" %%d in ('powershell -NoProfile -Command "Get-Date -Format HH:mm:ss"') do set "NOW=%%d"
echo [!NOW!] !MSG!
echo [!NOW!] !MSG!>>"%LOGFILE%"
exit /b 0

:progress
set /a "STEP=%~1"
set /a "TOTAL=%~2"
set /a "WIDTH=30"
set /a "FILLED=WIDTH*STEP/TOTAL"
set /a "PCT=100*STEP/TOTAL"
set "BAR="
for /l %%i in (1,1,%FILLED%) do set "BAR=!BAR!#"
set /a "EMPTY=WIDTH-FILLED"
for /l %%i in (1,1,%EMPTY%) do set "BAR=!BAR!."
echo [!BAR!] !PCT!%%
exit /b 0

:flushlog
if not exist "%PROJECT_ROOT%logs" mkdir "%PROJECT_ROOT%logs"
copy /y "%LOGFILE%" "%PROJECT_ROOT%logs\" >nul 2>&1
call :log "Log copied to %PROJECT_ROOT%logs\%%~nx0"
echo.
echo Full log: %LOGFILE%
exit /b 0

:fail
call :log "[ERROR] %~1"
call :flushlog
pause
exit /b 1