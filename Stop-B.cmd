@echo off
rem Stop only this project's local Gazebo simulation B (same as Start-B.cmd --stop).
setlocal
call "%~dp0sawyer_teleop\scripts\windows\run-python.cmd" -I -u "%~dp0sawyer_teleop\scripts\windows_sim_launcher.py" --stop
set "SAWYER_EXIT=%ERRORLEVEL%"
pause
exit /b %SAWYER_EXIT%
