# Execute este script em um PowerShell como Administrador, na pasta raiz do
# projeto clonado na VPS (a mesma que contem o `.env` desta instancia).
#
# Diferente da tarefa principal (Telegram MT5 Copier), que usa `-AtLogOn`
# porque o MetaTrader5 exige uma sessao grafica, o backup nao abre nenhum
# terminal MT5 nem precisa de tela - por isso esta tarefa roda com
# `LogonType Password`, funcionando mesmo sem ninguem logado por RDP depois
# de um reboot. O script pede a senha da conta interativamente (nunca fica
# salva em texto neste arquivo nem em nenhum log).
[CmdletBinding()]
param(
    [string]$TaskName = '',
    [string]$Time = '03:00'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $projectRoot '.env'
$launcher = Join-Path $PSScriptRoot 'backup_vps.ps1'
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
if ([string]::IsNullOrWhiteSpace($TaskName)) {
    $TaskName = if ($instanceId -eq 'main') {
        'Instituto Trader - Backup'
    } else {
        "Instituto Trader - Backup - $instanceId"
    }
}

if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    throw "Arquivo .env nao encontrado em $projectRoot. Configure BACKUP_ENCRYPTION_KEY e B2_* antes de instalar esta tarefa."
}
if ([string]::IsNullOrWhiteSpace((Get-DotEnvValue -Name 'BACKUP_ENCRYPTION_KEY' -DefaultValue ''))) {
    throw "BACKUP_ENCRYPTION_KEY ausente no .env. Gere uma com: .\.venv\Scripts\python.exe -m telegram_mt5_copier.backup --generate-key"
}
if ([string]::IsNullOrWhiteSpace((Get-DotEnvValue -Name 'B2_BUCKET_NAME' -DefaultValue ''))) {
    throw "B2_KEY_ID / B2_APPLICATION_KEY / B2_BUCKET_NAME ausentes no .env."
}

Write-Host "A tarefa vai rodar como '$currentUser', mesmo sem sessao RDP aberta."
$credential = Get-Credential -UserName $currentUser -Message "Senha desta conta do Windows (para a tarefa rodar sem logon interativo)"

$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`""
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument $arguments `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -User $credential.UserName `
    -Password $credential.GetNetworkCredential().Password `
    -RunLevel Highest `
    -Description "Backup diario criptografado da instancia $instanceId para o Backblaze B2." `
    -Force | Out-Null

Write-Host "Tarefa '$TaskName' instalada - roda todo dia as $Time, mesmo sem RDP aberto."
Write-Host "Para testar agora: Start-ScheduledTask -TaskName '$TaskName'"
