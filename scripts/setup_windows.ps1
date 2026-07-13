$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

function Test-Python312 {
    param(
        [string]$Command,
        [string[]]$Arguments
    )

    try {
        & $Command @Arguments -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" | Out-Null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

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

$PythonCommand = $null
$PythonArguments = @()

if (Test-Python312 -Command "py" -Arguments @("-3.12")) {
    $PythonCommand = "py"
    $PythonArguments = @("-3.12")
}
elseif (Test-Python312 -Command "python" -Arguments @()) {
    $PythonCommand = "python"
}
elseif (Test-Python312 -Command "python3" -Arguments @()) {
    $PythonCommand = "python3"
}
else {
    Write-Host "Python 3.12 ou superior nao foi encontrado." -ForegroundColor Red
    Write-Host "Instale o Python 3.12 no Windows Server e execute este script novamente."
    exit 1
}

& $PythonCommand @PythonArguments -m venv ".venv"

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "Nao foi possivel encontrar o Python do ambiente virtual em .venv\Scripts\python.exe." -ForegroundColor Red
    exit 1
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r "requirements.txt"
& $VenvPython -m pip install -e "."

$DataDir = Convert-ToProjectPath (Get-DotEnvValue -Name "DATA_DIR" -DefaultValue "./data")
$SessionDir = Convert-ToProjectPath (Get-DotEnvValue -Name "SESSION_DIR" -DefaultValue "./sessions")
$LogDir = Convert-ToProjectPath (Get-DotEnvValue -Name "LOG_DIR" -DefaultValue "./logs")

foreach ($Directory in @($DataDir, $SessionDir, $LogDir)) {
    New-Item -ItemType Directory -Force -Path $Directory | Out-Null
}

if (-not (Test-Path (Join-Path $ProjectRoot ".env"))) {
    Write-Host ""
    Write-Host "Arquivo .env nao encontrado." -ForegroundColor Yellow
    Write-Host "Crie uma copia de .env.example como .env diretamente nesta VPS."
    Write-Host "Preencha API ID, API Hash e chats somente na VPS. Nao envie esses dados ao GitHub."
}

Write-Host ""
Write-Host "Setup do Windows concluido."
Write-Host "Pastas preparadas:"
Write-Host "DATA_DIR=$DataDir"
Write-Host "SESSION_DIR=$SessionDir"
Write-Host "LOG_DIR=$LogDir"
