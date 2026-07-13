$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

function Get-PythonVersion {
    param(
        [string]$Command,
        [string[]]$Arguments
    )

    try {
        $VersionText = & $Command @Arguments -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $VersionText) {
            return $null
        }

        $Version = [version]$VersionText.Trim()
        if ($Version.Major -gt 3 -or ($Version.Major -eq 3 -and $Version.Minor -ge 12)) {
            return $Version
        }

        return $null
    }
    catch {
        return $null
    }
}

function Add-PythonCandidate {
    param(
        [System.Collections.ArrayList]$Candidates,
        [string]$Command,
        [string[]]$Arguments,
        [string]$Label
    )

    $Version = Get-PythonVersion -Command $Command -Arguments $Arguments
    if ($null -ne $Version) {
        [void]$Candidates.Add([PSCustomObject]@{
            Command = $Command
            Arguments = $Arguments
            Version = $Version
            Label = $Label
        })
    }
}

function Get-PythonCandidates {
    $Candidates = [System.Collections.ArrayList]::new()

    if (Get-Command "py" -ErrorAction SilentlyContinue) {
        try {
            $PyList = & py -0p 2>$null
            foreach ($Line in $PyList) {
                if ($Line -match "(?<Path>[A-Za-z]:\\.*python(?:\.exe)?)\s*$") {
                    $PythonPath = $Matches.Path.Trim()
                    if (Test-Path $PythonPath) {
                        Add-PythonCandidate -Candidates $Candidates -Command $PythonPath -Arguments @() -Label $PythonPath
                    }
                }
            }
        }
        catch {
            # Fallback candidates below still cover common installations.
        }

        Add-PythonCandidate -Candidates $Candidates -Command "py" -Arguments @("-3") -Label "py -3"
        Add-PythonCandidate -Candidates $Candidates -Command "py" -Arguments @("-3.13") -Label "py -3.13"
        Add-PythonCandidate -Candidates $Candidates -Command "py" -Arguments @("-3.12") -Label "py -3.12"
    }

    Add-PythonCandidate -Candidates $Candidates -Command "python" -Arguments @() -Label "python"
    Add-PythonCandidate -Candidates $Candidates -Command "python3" -Arguments @() -Label "python3"

    return $Candidates
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

$PythonCandidates = Get-PythonCandidates
$SelectedPython = $PythonCandidates | Sort-Object -Property Version -Descending | Select-Object -First 1

if ($null -eq $SelectedPython) {
    Write-Host "Python 3.12 ou superior nao foi encontrado." -ForegroundColor Red
    Write-Host "Instale o Python 3.12, 3.13 ou superior no Windows Server e execute este script novamente."
    exit 1
}

Write-Host "Python selecionado: $($SelectedPython.Label) ($($SelectedPython.Version))"
& $SelectedPython.Command @($SelectedPython.Arguments) -m venv ".venv"

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "Nao foi possivel encontrar o Python do ambiente virtual em .venv\Scripts\python.exe." -ForegroundColor Red
    exit 1
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r "requirements.txt"
& $VenvPython -m pip install -e "."
& $VenvPython -m pytest -v

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
