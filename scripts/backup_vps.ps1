# Faz o backup criptografado desta instancia e envia para o Backblaze B2.
# Chamado pela Tarefa Agendada de backup (separada da tarefa que sobe o
# supervisor) — ver README, secao "Backup". Rode a partir da pasta raiz do
# projeto clonado na VPS (a mesma pasta que contem o `.env` desta instancia).

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
    Write-Host "Arquivo .env nao encontrado em $ProjectRoot." -ForegroundColor Red
    exit 1
}

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "Ambiente virtual nao encontrado. Execute scripts\setup_windows.ps1 primeiro." -ForegroundColor Red
    exit 1
}

$LogDir = Convert-ToProjectPath (Get-DotEnvValue -Name "LOG_DIR" -DefaultValue "./logs")
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$StdoutLog = Join-Path $LogDir "backup-run-$Timestamp.out.log"
$StderrLog = Join-Path $LogDir "backup-run-$Timestamp.err.log"

Write-Host "Iniciando backup de $ProjectRoot."
Write-Host "stdout: $StdoutLog"
Write-Host "stderr: $StderrLog"

# Mesma razao do start_windows.ps1: o logger da aplicacao escreve linhas
# INFO/WARNING em stderr normalmente — nao sao falhas. So o codigo de saida
# (checado logo abaixo) decide sucesso ou falha de verdade.
$PreviousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $VenvPython -m telegram_mt5_copier.backup 1>> $StdoutLog 2>> $StderrLog
$ExitCode = $LASTEXITCODE
$ErrorActionPreference = $PreviousErrorActionPreference

if ($ExitCode -ne 0) {
    Write-Host "Backup falhou com codigo $ExitCode. Consulte $StderrLog." -ForegroundColor Red
} else {
    Write-Host "Backup concluido." -ForegroundColor Green
}

exit $ExitCode
