@echo off
rem Offline checks, no robot. Reports go to windows-local\artifacts\.
rem   Test.cmd                 full pytest suite
rem   Test.cmd ik              local IK check
rem   Test.cmd lab-scene       replay fixed 7,177-input lab scene
rem   Test.cmd feedback-delay  feedback deadline comparison
rem   Test.cmd env             runtime / dependency / adb self-check
setlocal
call "%~dp0sawyer_teleop\scripts\windows\run-python.cmd" -I -u "%~dp0sawyer_teleop\scripts\windows_dev.py" test %*
set "SAWYER_EXIT=%ERRORLEVEL%"
pause
exit /b %SAWYER_EXIT%
