@echo off
rem Start operator A (local Web console). Options: --port N, --no-browser.
setlocal
call "%~dp0sawyer_teleop\scripts\windows\run-python.cmd" -I "%~dp0sawyer_teleop\scripts\windows_launcher.py" web %*
set "SAWYER_EXIT=%ERRORLEVEL%"
if not "%SAWYER_EXIT%"=="0" pause
exit /b %SAWYER_EXIT%
