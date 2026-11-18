#Requires -Version 5.1
<#
 Prepares the Python side: virtual environment, dependencies, migrations, seed data, tests.
 Run from anywhere:  .\deploy\setup.ps1   (safe to run again after every update)
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Invoke-Step($label, [scriptblock]$cmd) {
    Write-Host "==> $label" -ForegroundColor Cyan
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "Step failed: $label" }
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python not found. Install Python 3.12 from python.org (tick 'Add to PATH')."
}
$ver = & python -c "import sys; print('{0}.{1}'.format(sys.version_info[0], sys.version_info[1]))"
if ([version]$ver -lt [version]"3.11") { throw "Python 3.11 or newer is required (found $ver)." }

if (-not (Test-Path ".env")) {
    Copy-Item ".env.template" ".env"
    Write-Warning ".env was created from the template. Fill in the passwords (or run deploy\create_database.ps1 first), then run this script again."
    exit 0
}
if ((Get-Content ".env" -Raw) -match "CHANGE_ME") {
    throw ".env still contains CHANGE_ME. Fill in the real database passwords first."
}

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Invoke-Step "Create virtual environment" { python -m venv .venv } }
Invoke-Step "Install dependencies" { & $py -m pip install --quiet --upgrade pip; & $py -m pip install --quiet -r requirements.txt }
Invoke-Step "Apply database migrations" { & $py -m alembic upgrade head }
Invoke-Step "Seed report types and default settings" { & $py -m database.seed }
Invoke-Step "Run Phase 1 tests" { & $py -m pytest -q tests }
Write-Host "Phase 1 foundation is ready." -ForegroundColor Green
