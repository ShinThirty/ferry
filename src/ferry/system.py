"""What ferry asks of the operating system besides git and age: this machine's
name, whether a process is alive, a detached rerun of itself, and renames and
deletes that wait out another handle on the file."""
from __future__ import annotations

import errno
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE          # else a 64-bit handle is cut to 32 bits
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _k32.GetStdHandle.restype = wintypes.HANDLE
    _k32.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _QUERY = 0x1000                 # PROCESS_QUERY_LIMITED_INFORMATION
    _DENIED = 5                     # ERROR_ACCESS_DENIED: the process is there, just not ours to open
    _STILL_ACTIVE = 259


def default_host() -> str:
    try:
        p = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    except FileNotFoundError:
        pass
    return socket.gethostname().split(".")[0]


def _probe(pid: int) -> tuple[bool, str]:
    """Whether pid is running, and when it started where the OS says so
    exactly ("" where it doesn't: macOS has no /proc)."""
    if os.name == "nt":
        # os.kill(pid, 0) is no test here: signal 0 is CTRL_C_EVENT, sent to a process group.
        h = _k32.OpenProcess(_QUERY, False, pid)
        if not h:
            return ctypes.get_last_error() == _DENIED, ""
        try:
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
                return False, ""
            times = [wintypes.FILETIME() for _ in range(4)]          # creation, exit, kernel, user
            if not _k32.GetProcessTimes(h, *map(ctypes.byref, times)):
                return True, ""
            return True, str(times[0].dwHighDateTime << 32 | times[0].dwLowDateTime)
        finally:
            _k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, ""
    except PermissionError:
        pass
    try:
        return True, Path(f"/proc/{pid}/stat").read_bytes().rpartition(b")")[2].split()[19].decode()
    except (OSError, IndexError):
        return True, ""


def started(pid: int) -> str:
    """When pid started, as alive() compares it; "" if the OS won't say."""
    return _probe(pid)[1]


def alive(pid: int, start: str = "") -> bool:
    """Whether pid is running and, given when it started, is still that process
    rather than a later one handed the same number. Windows reuses them soon."""
    up, now = _probe(pid)
    return up and not (start and now and now != start)


def spawn_detached(args: Sequence[str], log: Path) -> None:
    """Run ferry with args in a session of its own, appending its output to log,
    so it outlives the terminal, commit or Claude session that started it. -I
    keeps the folder it starts in off the import path: a ferry.py there is
    the project's, not this one."""
    with open(log, "ab") as out:
        def start(**kw) -> None:
            subprocess.Popen([sys.executable, "-I", "-m", "ferry", *args], stdin=subprocess.DEVNULL,
                             stdout=out, stderr=subprocess.STDOUT, **kw)
        if os.name != "nt":
            start(start_new_session=True)
            return
        # A console of its own with no window, so closing the one that started it
        # doesn't end it and the git it runs flashes no windows; a process group of
        # its own, so Ctrl+C there doesn't reach it; and out of any job that kills
        # its members when it closes, as uv run's does, where the job allows that.
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            start(creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB)
        except PermissionError:
            # Still in the job: if it kills its members on closing, as uv run's does,
            # this run ends with whatever started it. Say so where its output goes.
            out.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  warn  a job won't let this run leave it, so "
                      "it may end with what started it — the next run does its work\n".encode())
            out.flush()
            start(creationflags=flags)


def retry(op: Callable[..., object], *args) -> None:
    """op(*args), retried for a couple of seconds while another handle on the file
    is in the way: on Windows, any open without share-delete blocks a rename over
    the file or its removal — a reader on another machine, Defender, the indexer.
    Over SMB, macOS and Linux see the server's refusal as EBUSY."""
    for _ in range(9):
        try:
            op(*args)
            return
        except OSError as e:
            if not isinstance(e, PermissionError) and e.errno != errno.EBUSY:
                raise
        time.sleep(0.2)
    op(*args)


def onedrive(path: Path) -> str | None:
    """The OneDrive folder path lies in, if any. OneDrive uploads what's in it as
    it is, and Windows can move Documents into it (Known Folder Move)."""
    target = Path(os.path.normcase(os.path.realpath(path)))
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = os.environ.get(var)
        if root and target.is_relative_to(os.path.normcase(os.path.realpath(root))):
            return root
    return None


def ansi_colours() -> bool:
    """Whether stdout is a terminal that shows colour codes as colour. Windows'
    console shows them only once asked to."""
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    h, mode = _k32.GetStdHandle(-11), wintypes.DWORD()          # STD_OUTPUT_HANDLE
    if not _k32.GetConsoleMode(h, ctypes.byref(mode)):
        return False
    return bool(mode.value & 4 or _k32.SetConsoleMode(h, mode.value | 4))    # VIRTUAL_TERMINAL_PROCESSING
