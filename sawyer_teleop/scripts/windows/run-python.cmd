@echo off
rem Internal: run a script with the sibling windows-runtime Python.
setlocal
chcp 65001 >nul
cd /d "%~dp0..\.."
set "SAWYER_PYTHON=%CD%\..\windows-runtime\python\python.exe"
if not exist "%SAWYER_PYTHON%" set "SAWYER_PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%SAWYER_PYTHON%" (
  echo Python runtime not found. Run Setup.cmd in the repository root first.
  exit /b 1
)
"%SAWYER_PYTHON%" %*
exit /b %ERRORLEVEL%
