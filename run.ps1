$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv run main.py
} elseif (Test-Path ".\.venv\Scripts\python.exe") {
    & ".\.venv\Scripts\python.exe" ".\src\main.py"
} else {
    Write-Host "Virtual environment not found." -ForegroundColor Red
    Write-Host "Run .\install.ps1 or 'uv sync' first."
    exit 1
}
