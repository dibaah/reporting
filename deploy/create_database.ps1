#Requires -Version 5.1
<#
 One-time database creation. Run on the machine that hosts PostgreSQL, from the repo root:
   .\deploy\create_database.ps1
 Creates database report_intel with two roles (report_owner for migrations, report_app for
 runtime), then writes .env with the connection strings if .env does not exist yet.
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Find-Psql {
    $cmd = Get-Command psql -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $hit = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\psql.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($hit) { return $hit.FullName }
    throw "psql.exe not found. Install PostgreSQL 16 or add its bin folder to PATH."
}
function Read-Plain($prompt) {
    $s = Read-Host $prompt -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

$psql = Find-Psql
$pgHost = Read-Host "PostgreSQL host [localhost]"; if (-not $pgHost) { $pgHost = "localhost" }
$pgPort = Read-Host "PostgreSQL port [5432]";      if (-not $pgPort) { $pgPort = "5432" }
$superPw = Read-Plain "Password of the 'postgres' superuser"
$ownerPw = Read-Plain "Choose a password for report_owner (migrations)"
$appPw   = Read-Plain "Choose a password for report_app (runtime)"

$env:PGPASSWORD = $superPw
try {
    & $psql -h $pgHost -p $pgPort -U postgres -v ON_ERROR_STOP=1 `
        -v "owner_pw=$ownerPw" -v "app_pw=$appPw" -f "database\sql\create_database.sql"
    if ($LASTEXITCODE -ne 0) { throw "Database creation failed (see messages above)." }
} finally { Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue }

if (-not (Test-Path ".env")) {
    $o = [uri]::EscapeDataString($ownerPw); $a = [uri]::EscapeDataString($appPw)
    (Get-Content ".env.template") |
        ForEach-Object {
            $_ -replace "^DATABASE_URL=.*", "DATABASE_URL=postgresql+psycopg://report_app:$a@${pgHost}:${pgPort}/report_intel" `
               -replace "^MIGRATION_DATABASE_URL=.*", "MIGRATION_DATABASE_URL=postgresql+psycopg://report_owner:$o@${pgHost}:${pgPort}/report_intel"
        } | Set-Content ".env" -Encoding UTF8
    Write-Host ".env created. Check RAW_STORE_PATH and LOG_DIR in it."
} else {
    Write-Warning ".env already exists and was not changed. Update the passwords in it yourself."
}
Write-Host "Done. Next: .\deploy\setup.ps1"
