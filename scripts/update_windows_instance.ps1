# Atualiza esta copia do projeto e reinicia somente a sua instancia.
[CmdletBinding()]
param(
    [string]$TaskName = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$stopper = Join-Path $PSScriptRoot 'stop_windows_instance.ps1'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python do ambiente virtual nao encontrado em $python."
}

& $stopper -TaskName $TaskName

$resolvedTaskName = $TaskName
if ([string]::IsNullOrWhiteSpace($resolvedTaskName)) {
    $envPath = Join-Path $projectRoot '.env'
    $instanceId = 'main'
    if (Test-Path -LiteralPath $envPath -PathType Leaf) {
        $line = Get-Content -LiteralPath $envPath |
            Where-Object { $_ -match '^\s*INSTANCE_ID=' } |
            Select-Object -Last 1
        if ($line) { $instanceId = (($line -split '=', 2)[1]).Trim().Trim('"').Trim("'") }
    }
    $resolvedTaskName = if ($instanceId -eq 'main') {
        'Telegram MT5 Copier'
    } else {
        "Telegram MT5 Copier - $instanceId"
    }
}

try {
    Push-Location -LiteralPath $projectRoot
    & git pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "git pull falhou com codigo $LASTEXITCODE." }

    & $python -m pip install -e .
    if ($LASTEXITCODE -ne 0) { throw "Instalacao falhou com codigo $LASTEXITCODE." }

    & $python -c "import telegram_mt5_copier; print('versao=', telegram_mt5_copier.__version__)"
}
finally {
    Pop-Location
    if (Get-ScheduledTask -TaskName $resolvedTaskName -ErrorAction SilentlyContinue) {
        Start-ScheduledTask -TaskName $resolvedTaskName
        Write-Host "Tarefa iniciada: $resolvedTaskName"
    } else {
        Write-Warning "Tarefa nao encontrada: $resolvedTaskName"
    }
}
