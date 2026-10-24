@echo off
REM Restarts the FarazHonarPriceUpdater service (run as Administrator).
setlocal EnableExtensions

set "SERVICE_NAME=FarazHonarPriceUpdater"
pushd "%~dp0.."
set "PROJECT_DIR=%CD%"
popd
set "NSSM=%PROJECT_DIR%\deploy\nssm.exe"

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Run this script as Administrator.
    exit /b 1
)
if not exist "%NSSM%" (
    echo [ERROR] nssm.exe not found at "%NSSM%".
    exit /b 1
)

echo Restarting %SERVICE_NAME% ...
"%NSSM%" restart "%SERVICE_NAME%"
if errorlevel 1 (
    echo [ERROR] Restart failed. Is the service installed? Check: sc query %SERVICE_NAME%
    exit /b 1
)

"%NSSM%" status "%SERVICE_NAME%"
echo Check %PROJECT_DIR%\logs\faraz-honar.log for the startup lines.
endlocal
exit /b 0
