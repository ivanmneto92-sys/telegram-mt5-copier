$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

function Get-DotEnvValue {
    param(
        [string]$Name,
        [string]$DefaultValue
    )

    $EnvPath = Join-Path $ProjectRoot ".env"
    if (Test-Path $EnvPath) {
        foreach ($Line in Get-Content -LiteralPath $EnvPath -Encoding UTF8) {
            $Trimmed = $Line.Trim()
            if ($Trimmed.Length -eq 0 -or $Trimmed.StartsWith("#")) {
                continue
            }

            $Index = $Trimmed.IndexOf("=")
            if ($Index -lt 1) {
                continue
            }

            $Key = $Trimmed.Substring(0, $Index).Trim()
            $Value = $Trimmed.Substring($Index + 1).Trim()
            if (($Value.StartsWith('"') -and $Value.EndsWith('"')) -or ($Value.StartsWith("'") -and $Value.EndsWith("'"))) {
                $Value = $Value.Substring(1, $Value.Length - 2)
            }

            if ($Key -eq $Name -and $Value.Length -gt 0) {
                return $Value
            }
        }
    }

    return $DefaultValue
}

function Convert-ToProjectPath {
    param([string]$Value)

    if ([System.IO.Path]::IsPathRooted($Value)) {
        return $Value
    }

    return Join-Path $ProjectRoot $Value
}

$EnvPath = Join-Path $ProjectRoot ".env"
if (-not (Test-Path $EnvPath)) {
    Write-Host "Arquivo .env nao encontrado. Crie o arquivo a partir de .env.example diretamente na VPS." -ForegroundColor Red
    exit 1
}

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ActivateScript = Join-Path $ProjectRoot ".venv\Scripts\Activate.ps1"

if (-not (Test-Path $VenvPython)) {
    Write-Host "Ambiente virtual nao encontrado. Execute scripts\setup_windows.ps1 primeiro." -ForegroundColor Red
    exit 1
}

if (Test-Path $ActivateScript) {
    . $ActivateScript
}

$LogDir = Convert-ToProjectPath (Get-DotEnvValue -Name "LOG_DIR" -DefaultValue "./logs")
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$StdoutLog = Join-Path $LogDir "start-$Timestamp.out.log"
$StderrLog = Join-Path $LogDir "start-$Timestamp.err.log"

Write-Host "Iniciando aplicacao."
Write-Host "stdout: $StdoutLog"
Write-Host "stderr: $StderrLog"

& $VenvPython -m telegram_mt5_copier 1>> $StdoutLog 2>> $StderrLog
$ExitCode = $LASTEXITCODE

if ($ExitCode -ne 0) {
    Write-Host "Aplicacao falhou com codigo $ExitCode. Consulte os logs acima." -ForegroundColor Red
}

exit $ExitCode
