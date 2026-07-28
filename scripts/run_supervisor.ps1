$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$supervisor = Join-Path $projectRoot '.venv\Scripts\telegram-mt5-supervisor.exe'

if (-not (Test-Path -LiteralPath $supervisor -PathType Leaf)) {
    throw "Supervisor nao encontrado em $supervisor. Execute a instalacao do projeto primeiro."
}

Set-Location -LiteralPath $projectRoot
& $supervisor
exit $LASTEXITCODE
