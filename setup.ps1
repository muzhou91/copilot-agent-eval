# One-time setup for Copilot Agent Eval on Windows.
# Usage: powershell -ExecutionPolicy Bypass -File setup.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "==> Creating Python virtual environment (.venv)..." -ForegroundColor Cyan
python -m venv .venv
& .\.venv\Scripts\Activate.ps1

Write-Host "==> Installing Python dependencies..." -ForegroundColor Cyan
pip install --upgrade pip
pip install -r requirements.txt

Write-Host "==> Installing Playwright Chromium browser..." -ForegroundColor Cyan
playwright install chromium

if (-not (Test-Path config.yaml)) { Copy-Item config.example.yaml config.yaml }
if (-not (Test-Path cases.csv))  { Copy-Item cases.example.csv cases.csv }

Write-Host ""
Write-Host "Setup complete. Next steps:" -ForegroundColor Green
Write-Host "  1. Edit config.yaml and cases.csv"
Write-Host "  2. `$env:DL_SECRET = `"your Direct Line secret`""
Write-Host "  3. .\.venv\Scripts\Activate.ps1"
Write-Host "  4. python -m copilot_agent_eval --auth-only"
Write-Host "  5. python -m copilot_agent_eval -v"
