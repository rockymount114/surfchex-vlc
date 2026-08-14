$ErrorActionPreference = "Stop"

Write-Host "=== SurfChex VLC installer ===" -ForegroundColor Cyan

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python was not found in PATH." -ForegroundColor Red
    Write-Host "Install Python 3.11+ and enable 'Add Python to PATH'."
    exit 1
}

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".\.venv\Scripts\python.exe" -m playwright install chromium

Write-Host ""
Write-Host "Installation complete." -ForegroundColor Green
Write-Host "Edit config.yaml before running."
Write-Host "Then run: .\run.ps1"
