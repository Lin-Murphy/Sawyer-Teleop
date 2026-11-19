@echo off
rem Start operator A (local Web console). Options: --port N, --no-browser.
setlocal
if /i "%~1"=="camera-compare" goto camera_compare
call "%~dp0sawyer_teleop\scripts\windows\run-python.cmd" -I "%~dp0sawyer_teleop\scripts\windows_launcher.py" web %*
set "SAWYER_EXIT=%ERRORLEVEL%"
if not "%SAWYER_EXIT%"=="0" pause
exit /b %SAWYER_EXIT%

:camera_compare
call "%~dp0sawyer_teleop\scripts\windows\run-python.cmd" -I "%~dp0sawyer_teleop\scripts\windows_launcher.py" %*
exit /b %ERRORLEVEL%
