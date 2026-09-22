# Sobe a API do portal em modo de desenvolvimento local, 100% isolada da VPS.
#
# - Banco SQLite, sessoes e chave de criptografia ficam em
#   %LOCALAPPDATA%\instituto-trader-dev\<instancia>, fora do repositorio.
# - Token do Telegram falso, DRY_RUN ligado, kill-switch ligado e MT5 em simulacao:
#   nenhuma ordem e enviada e nenhum servico externo e chamado.
# - Nunca aponte este script para os diretorios C:\Apps\... da VPS.
[CmdletBinding()]
param(
    [ValidateSet('main', 'robo_braba')]
    [string]$Instance = 'main',
    [int]$Port = 0,
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot

if ($Port -eq 0) {
    $Port = if ($Instance -eq 'main') { 8090 } else { 8091 }
}

$devRoot = Join-Path $env:LOCALAPPDATA "instituto-trader-dev\$Instance"
if ($devRoot.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'A pasta de desenvolvimento nao pode ficar dentro do repositorio.'
}
New-Item -ItemType Directory -Force -Path $devRoot | Out-Null

if ([string]::IsNullOrWhiteSpace($Python)) {
    $venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $Python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { 'python' }
}

$envPath = Join-Path $devRoot '.env'
if (-not (Test-Path -LiteralPath $envPath)) {
    $key = & $Python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($key)) {
        throw 'Nao foi possivel gerar a chave local. Verifique se o pacote cryptography esta instalado.'
    }
    $brand = if ($Instance -eq 'main') { 'Instituto Trader (DEV)' } else { 'Robo Braba (DEV)' }

    # Template MT5 falso (so o arquivo precisa existir; nunca e executado de
    # verdade aqui). Sem isso, nenhuma corretora apareceria no catalogo e o
    # cadastro de conta pelo site rejeitaria qualquer corretora digitada.
    $templateDir = Join-Path $devRoot 'mt5_templates\HFM'
    New-Item -ItemType Directory -Force -Path $templateDir | Out-Null
    $terminalPath = Join-Path $templateDir 'terminal64.exe'
    if (-not (Test-Path -LiteralPath $terminalPath)) {
        Set-Content -LiteralPath $terminalPath -Value 'placeholder' -Encoding ascii
    }

    $lines = @(
        "INSTANCE_ID=$Instance",
        "BRAND_NAME=$brand",
        'TELEGRAM_BOT_TOKEN=000000:DEV_LOCAL_ONLY',
        "MT5_CREDENTIAL_KEY=$($key.Trim())",
        'BOT_ADMIN_IDS=',
        'DRY_RUN=true',
        'GLOBAL_EXECUTION_KILL_SWITCH=true',
        'MT5_EXECUTION_MODE=simulation',
        'ALLOW_LIVE_ACCOUNTS=false',
        "MT5_BROKER_TEMPLATES=HFM=$templateDir",
        'MT5_BROKER_SERVERS=HFM=HFM-Demo|HFM-Live1',
        'MARKET_NEWS_ENABLED=false',
        'OPERATIONAL_ALERTS_ENABLED=false',
        'ONBOARDING_HOST=127.0.0.1',
        "ONBOARDING_PORT=$Port"
    )
    [System.IO.File]::WriteAllLines($envPath, $lines, [System.Text.UTF8Encoding]::new($false))
    Write-Host "Ambiente local criado em $devRoot" -ForegroundColor Green
}

Write-Host "API local ($Instance) em http://127.0.0.1:$Port  |  dados: $devRoot" -ForegroundColor Cyan
$env:PYTHONPATH = Join-Path $projectRoot 'src'
Set-Location $devRoot
& $Python -m telegram_mt5_copier.web_server
