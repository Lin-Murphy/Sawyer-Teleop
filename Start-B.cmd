@echo off
rem Start local Gazebo simulation B (needs Docker Desktop and gazebo-image\).
rem   Start-B.cmd [--headless]   start
rem   Start-B.cmd --stop         stop
rem   Start-B.cmd --check        check only
setlocal
call "%~dp0sawyer_teleop\scripts\windows\run-python.cmd" -I -u "%~dp0sawyer_teleop\scripts\windows_sim_launcher.py" %*
set "SAWYER_EXIT=%ERRORLEVEL%"
if not "%SAWYER_EXIT%"=="0" pause
exit /b %SAWYER_EXIT%
