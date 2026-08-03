[CmdletBinding()]
param(
    [string]$TaskName = ''
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $projectRoot '.env'
if ([string]::IsNullOrWhiteSpace($TaskName)) {
    $instanceId = 'main'
    if (Test-Path -LiteralPath $envPath -PathType Leaf) {
        $line = Get-Content -LiteralPath $envPath |
            Where-Object { $_ -match '^\s*INSTANCE_ID=' } |
            Select-Object -Last 1
        if ($line) { $instanceId = (($line -split '=', 2)[1]).Trim().Trim('"').Trim("'") }
    }
    $TaskName = if ($instanceId -eq 'main') {
        'Telegram MT5 Copier'
    } else {
        "Telegram MT5 Copier - $instanceId"
    }
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Tarefa '$TaskName' removida."
}
else {
    Write-Host "Tarefa '$TaskName' nao estava instalada."
}
