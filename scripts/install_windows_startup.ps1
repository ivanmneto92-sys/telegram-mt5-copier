# Execute este script em um PowerShell como Administrador.
[CmdletBinding()]
param(
    [string]$TaskName = 'Telegram MT5 Copier'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot 'run_supervisor.ps1'
$supervisor = Join-Path $projectRoot '.venv\Scripts\telegram-mt5-supervisor.exe'
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

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
    -Description 'Inicia e supervisiona Bot, Mini App, Monitor de sinais e Worker MT5.' `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Host "Tarefa '$TaskName' instalada e iniciada para o usuario $currentUser."
Write-Host "Nao inicie os quatro servicos manualmente enquanto o supervisor estiver ativo."
