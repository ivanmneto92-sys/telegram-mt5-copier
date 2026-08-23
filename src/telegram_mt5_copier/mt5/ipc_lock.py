from __future__ import annotations

import os
from pathlib import Path
import sys
import time


class MT5OperationLock:
    def __init__(self, account_dir: Path, timeout_seconds: float = 70.0) -> None:
        self.path = account_dir / "mt5_api.lock"
        self.timeout_seconds = timeout_seconds
        self._fd: int | None = None

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(self._fd, str(os.getpid()).encode("ascii"))
                return
            except FileExistsError:
                if self._remove_if_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("mt5_account_busy")
                time.sleep(0.2)

    def _remove_if_stale(self) -> bool:
        try:
            pid = int(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0
        if pid and process_is_running(pid):
            return False
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        return True

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "MT5OperationLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def terminate_process_by_path(exe_path: Path) -> bool:
    """Force-closes any running process launched from exe_path.

    MetaTrader5.shutdown() only closes the Python IPC connection; it never
    terminates the terminal64.exe process itself. Deleting a client's MT5
    account needs to actually close their terminal so the VPS frees the RAM,
    so this walks the process list looking for a match on the full image
    path (matching by name alone would hit terminal64.exe from other
    accounts). Windows-only: every isolated terminal copy is launched
    on Windows, so there is nothing to terminate elsewhere.
    """
    if sys.platform != "win32":
        return False

    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_TERMINATE = 0x0001

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:
        return False

    target_name = exe_path.name.lower()
    target_resolved = str(exe_path.resolve()).lower()
    terminated = False
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        found = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.szExeFile.decode("mbcs", errors="ignore").lower() == target_name:
                pid = entry.th32ProcessID
                handle = kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE, False, pid
                )
                if handle:
                    buffer_size = wintypes.DWORD(1024)
                    buffer = ctypes.create_unicode_buffer(buffer_size.value)
                    if kernel32.QueryFullProcessImageNameW(
                        handle, 0, buffer, ctypes.byref(buffer_size)
                    ) and buffer.value.lower() == target_resolved:
                        kernel32.TerminateProcess(handle, 0)
                        kernel32.WaitForSingleObject(handle, 5000)
                        terminated = True
                    kernel32.CloseHandle(handle)
            found = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return terminated
