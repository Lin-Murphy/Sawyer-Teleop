# Download the Windows runtime that is not stored in git:
#   ..\windows-runtime\python          Python 3.11 x64 (python.org NuGet package)
#   ..\windows-runtime\platform-tools  Android adb (Google)
# Existing folders are kept. Python packages are installed by windows_dev.py setup.
param([string]$Runtime)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Project = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
if (-not $Runtime) { $Runtime = Join-Path (Split-Path $Project -Parent) "windows-runtime" }
$PythonVersion = "3.11.9"
$PythonUrl = "https://www.nuget.org/api/v2/package/python/$PythonVersion"
$AdbUrl = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"

function Get-Archive($Url, $Name) {
    $temp = Join-Path ([IO.Path]::GetTempPath()) ("sawyer-" + [Guid]::NewGuid())
    New-Item -ItemType Directory $temp | Out-Null
    $zip = Join-Path $temp "$Name.zip"
    Write-Host "Downloading $Url"
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath (Join-Path $temp "x")
    return $temp
}

New-Item -ItemType Directory -Force $Runtime | Out-Null
$python = Join-Path $Runtime "python\python.exe"
if (Test-Path $python) {
    Write-Host "Python already present: $python"
} else {
    $temp = Get-Archive $PythonUrl "python"
    Move-Item (Join-Path $temp "x\tools") (Join-Path $Runtime "python")
    Remove-Item -Recurse -Force $temp
    & $python -m ensurepip --upgrade
    if ($LASTEXITCODE) { throw "ensurepip failed" }
}
$adb = Join-Path $Runtime "platform-tools\adb.exe"
if (Test-Path $adb) {
    Write-Host "adb already present: $adb"
} else {
    $temp = Get-Archive $AdbUrl "platform-tools"
    Move-Item (Join-Path $temp "x\platform-tools") (Join-Path $Runtime "platform-tools")
    Remove-Item -Recurse -Force $temp
}
Write-Host "Runtime ready: $Runtime"
