from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def stop_terminal_processes(terminal_path: Path, *, timeout_seconds: int = 10) -> int:
    """Stop only terminal64 processes whose executable matches ``terminal_path``."""
    if sys.platform != "win32":
        return 0

    environment = os.environ.copy()
    environment["TGCP_TERMINAL_PATH"] = str(terminal_path.resolve())
    script = r"""
$ErrorActionPreference = 'Stop'
$target = [IO.Path]::GetFullPath($env:TGCP_TERMINAL_PATH)
$processes = @(Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" |
    Where-Object {
        $_.ExecutablePath -and
        [IO.Path]::GetFullPath($_.ExecutablePath).Equals(
            $target,
            [StringComparison]::OrdinalIgnoreCase
        )
    })

foreach ($process in $processes) {
    Stop-Process -Id $process.ProcessId -ErrorAction Stop
}

foreach ($process in $processes) {
    Wait-Process -Id $process.ProcessId -Timeout %d -ErrorAction SilentlyContinue
    if (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
    }
}

Write-Output $processes.Count
""" % max(1, int(timeout_seconds))
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=max(15, timeout_seconds + 10),
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            "Nao foi possivel encerrar o terminal MT5 desta conta."
            + (f" Detalhe: {detail}" if detail else "")
        )
    try:
        return int(completed.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return 0
