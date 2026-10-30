@echo off
rem First-time setup: download Python 3.11 and adb into windows-runtime\, install deps, self-check.
rem   Setup.cmd          install or repair the environment
rem   Setup.cmd quest    install the pinned Quest APK over USB
setlocal
if /i "%~1"=="quest" (
  call "%~dp0sawyer_teleop\scripts\windows\run-python.cmd" -I "%~dp0sawyer_teleop\scripts\windows_launcher.py" install-quest %2 %3
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sawyer_teleop\scripts\windows\bootstrap.ps1" && call "%~dp0sawyer_teleop\scripts\windows\run-python.cmd" -I -u "%~dp0sawyer_teleop\scripts\windows_dev.py" setup
)
set "SAWYER_EXIT=%ERRORLEVEL%"
pause
exit /b %SAWYER_EXIT%
