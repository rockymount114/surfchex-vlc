$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Virtual environment not found." -ForegroundColor Red
    Write-Host "Run .\install.ps1 first."
    exit 1
}

& ".\.venv\Scripts\python.exe" ".\src\main.py"
