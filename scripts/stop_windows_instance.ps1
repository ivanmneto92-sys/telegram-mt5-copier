# Encerra somente os servicos da instancia localizada neste projeto.
[CmdletBinding()]
param(
    [string]$TaskName = '',
    [int]$TimeoutSeconds = 20
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Split-Path -Parent $PSScriptRoot).TrimEnd('\')
$envPath = Join-Path $projectRoot '.env'

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

if ([string]::IsNullOrWhiteSpace($TaskName)) {
    $instanceId = Get-DotEnvValue -Name 'INSTANCE_ID' -DefaultValue 'main'
    $TaskName = if ($instanceId -eq 'main') {
        'Telegram MT5 Copier'
    } else {
        "Telegram MT5 Copier - $instanceId"
    }
}

$servicePattern = '(?i)(telegram-mt5-supervisor|telegram-mt5-onboarding|telegram-management-bot|telegram-copier|telegram-mt5-worker|telegram-mt5-health-monitor|telegram-market-news)(\.exe)?'
$rootPrefix = "$projectRoot\"

function Get-InstanceServiceProcesses {
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $commandLine = [string]$_.CommandLine
            $executablePath = [string]$_.ExecutablePath
            $belongsToInstance =
                $executablePath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
                $commandLine.IndexOf($projectRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
            $isService =
                ([string]$_.Name -match $servicePattern) -or
                ($commandLine -match $servicePattern) -or
                ($commandLine -match '(?i)run_supervisor\.ps1')
            $belongsToInstance -and $isService
        })
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    $processes = @(Get-InstanceServiceProcesses)
    if ($processes.Count -eq 0) { break }
    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $deadline)

$remaining = @(Get-InstanceServiceProcesses)
if ($remaining.Count -gt 0) {
    $details = ($remaining | ForEach-Object { "$($_.Name) PID=$($_.ProcessId)" }) -join ', '
    throw "Nao foi possivel encerrar todos os servicos da instancia: $details"
}

Write-Host "Instancia encerrada sem processos orfaos: $TaskName"
