# One-shot setup for Windows PowerShell:  .\scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host "Created .env - add your GOOGLE_API_KEY to it." }
.\.venv\Scripts\python.exe -m threadroute seed
Write-Host "`nSetup complete. Activate with:  .\.venv\Scripts\Activate.ps1"
Write-Host "Then try:  python -m threadroute demo     (no API key needed)"
