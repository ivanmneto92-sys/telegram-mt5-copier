# Execute este script em um PowerShell como Administrador.
[CmdletBinding()]
param(
    [string]$TaskName = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $projectRoot '.env'
$launcher = Join-Path $PSScriptRoot 'run_supervisor.ps1'
$supervisor = Join-Path $projectRoot '.venv\Scripts\telegram-mt5-supervisor.exe'
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Get-DotEnvValue {
    param([string]$Name, [string]$DefaultValue)
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
        return $DefaultValue
    }
    $line = Get-Content -LiteralPath $envPath |
        Where-Object { $_ -match "^\s*$([regex]::Escape($Name))=" } |
        Select-Object -Last 1
    if (-not $line) { return $DefaultValue }
    return (($line -split '=', 2)[1]).Trim().Trim('"').Trim("'")
}

$instanceId = Get-DotEnvValue -Name 'INSTANCE_ID' -DefaultValue 'main'
$brandName = Get-DotEnvValue -Name 'BRAND_NAME' -DefaultValue 'Instituto Trader'
if ([string]::IsNullOrWhiteSpace($TaskName)) {
    $TaskName = if ($instanceId -eq 'main') {
        'Telegram MT5 Copier'
    } else {
        "Telegram MT5 Copier - $instanceId"
    }
}

if (-not (Test-Path -LiteralPath $supervisor -PathType Leaf)) {
    throw "Supervisor nao encontrado. No Terminal 5, execute primeiro: .\.venv\Scripts\python.exe -m pip install -e ."
}

$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`""
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument $arguments `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Inicia e supervisiona a instancia $instanceId de $brandName." `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Host "Tarefa '$TaskName' instalada e iniciada para o usuario $currentUser."
Write-Host "Nao inicie os quatro servicos manualmente enquanto o supervisor estiver ativo."
