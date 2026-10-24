@echo off
REM ============================================================================
REM Installs Faraz-Honar as a Windows Service using NSSM.
REM Run from an elevated (Administrator) prompt. Safe to re-run: an existing
REM service is stopped and re-configured, not duplicated.
REM Does NOT start the service - test manually first (see docs\installation.md).
REM ============================================================================
setlocal EnableExtensions

set "SERVICE_NAME=FarazHonarPriceUpdater"
set "DISPLAY_NAME=Faraz Honar Price Updater"
set "DESCRIPTION=Faraz Honar multi-platform price updater: Telegram bot that updates prices on DigiKala, SnappShop, TapsiShop, Basalam and WooCommerce from a Google Sheet."

REM Project root = parent of this deploy folder
pushd "%~dp0.."
set "PROJECT_DIR=%CD%"
popd

set "NSSM=%PROJECT_DIR%\deploy\nssm.exe"
set "PYTHON=%PROJECT_DIR%\venv\Scripts\python.exe"
set "LOG_DIR=%PROJECT_DIR%\logs"

REM --- Preflight checks --------------------------------------------------------
net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Run this script as Administrator.
    exit /b 1
)
if not exist "%NSSM%" (
    echo [ERROR] nssm.exe not found at "%NSSM%". Copy the win64 nssm.exe into the deploy folder.
    exit /b 1
)
if not exist "%PYTHON%" (
    echo [ERROR] "%PYTHON%" not found. Create the venv and run: venv\Scripts\pip install -e .
    exit /b 1
)
if not exist "%PROJECT_DIR%\.env" (
    echo [ERROR] "%PROJECT_DIR%\.env" not found. Copy .env.example to .env and fill it in.
    exit /b 1
)
if not exist "%PROJECT_DIR%\config\sheet_mapping.yaml" (
    echo [ERROR] config\sheet_mapping.yaml is missing. Copy it to the server manually.
    exit /b 1
)
"%PYTHON%" -c "import faraz_honar" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] faraz_honar is not importable from the venv. Run: venv\Scripts\pip install -e .
    exit /b 1
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM --- Install (or re-configure) ----------------------------------------------
sc query "%SERVICE_NAME%" >nul 2>&1
if errorlevel 1 (
    echo Installing service %SERVICE_NAME% ...
    "%NSSM%" install "%SERVICE_NAME%" "%PYTHON%" -m faraz_honar.main
    if errorlevel 1 (
        echo [ERROR] nssm install failed.
        exit /b 1
    )
) else (
    echo Service %SERVICE_NAME% already exists - stopping and re-configuring it ...
    "%NSSM%" stop "%SERVICE_NAME%" >nul 2>&1
    "%NSSM%" set "%SERVICE_NAME%" Application "%PYTHON%"
    "%NSSM%" set "%SERVICE_NAME%" AppParameters "-m faraz_honar.main"
)

"%NSSM%" set "%SERVICE_NAME%" AppDirectory "%PROJECT_DIR%"
"%NSSM%" set "%SERVICE_NAME%" DisplayName "%DISPLAY_NAME%"
"%NSSM%" set "%SERVICE_NAME%" Description "%DESCRIPTION%"
"%NSSM%" set "%SERVICE_NAME%" Start SERVICE_AUTO_START

REM Restart on any exit (crash or clean exit), 5 seconds apart
"%NSSM%" set "%SERVICE_NAME%" AppExit Default Restart
"%NSSM%" set "%SERVICE_NAME%" AppRestartDelay 5000

REM Give the bot up to 15 s to shut down gracefully on stop/restart
"%NSSM%" set "%SERVICE_NAME%" AppStopMethodConsole 15000

REM Persian text in logs: force UTF-8 so stdout/stderr redirection never crashes
"%NSSM%" set "%SERVICE_NAME%" AppEnvironmentExtra PYTHONUTF8=1 PYTHONIOENCODING=utf-8

REM Service stdout/stderr capture with rotation (10 MB)
"%NSSM%" set "%SERVICE_NAME%" AppStdout "%LOG_DIR%\service-stdout.log"
"%NSSM%" set "%SERVICE_NAME%" AppStderr "%LOG_DIR%\service-stderr.log"
"%NSSM%" set "%SERVICE_NAME%" AppRotateFiles 1
"%NSSM%" set "%SERVICE_NAME%" AppRotateOnline 1
"%NSSM%" set "%SERVICE_NAME%" AppRotateBytes 10485760

echo.
echo Service %SERVICE_NAME% is installed (not started).
echo Start it with:  "%NSSM%" start %SERVICE_NAME%
echo Status:         "%NSSM%" status %SERVICE_NAME%
echo App log:        %LOG_DIR%\faraz-honar.log
endlocal
exit /b 0
