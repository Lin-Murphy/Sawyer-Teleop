@echo off
rem Start the standalone dual-camera comparison.
setlocal
call "%~dp0sawyer_teleop\scripts\windows\run-python.cmd" -I "%~dp0sawyer_teleop\scripts\windows_launcher.py" camera-compare %*
set "SAWYER_EXIT=%ERRORLEVEL%"
if "%~1"=="" if not "%SAWYER_EXIT%"=="0" pause
exit /b %SAWYER_EXIT%
