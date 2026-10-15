"""What can the worker reach? Run inside the worker before it is armed (`isolation.py`).

Each probe reports what happened (allowed, denied, absent, an error), never the content
of anything it managed to read: a file probe reads at most a few bytes and keeps none.
The server decides which ones must be refused for the install's level of isolation.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from collections.abc import Callable
from typing import Any

SECRET_WORDS = ("OPENCHART", "OANDA", "TELEGRAM", "TOKEN", "SECRET", "PASSWORD")
# The python image's GPG_KEY is the public fingerprint of Python's signing key (CI-0).
NOT_SECRET = {"GPG_KEY"}
READ_BYTES = 16


def attempt(fn: Callable[[], str]) -> dict[str, str]:
    try:
        return {"result": "allowed", "detail": fn()}
    except TimeoutError as exc:
        return {"result": "timed out", "detail": str(exc)}
    except PermissionError as exc:
        return {"result": "denied", "detail": f"{type(exc).__name__}: {exc.strerror or exc}"}
    except FileNotFoundError as exc:
        return {"result": "absent", "detail": f"{type(exc).__name__}: {exc.strerror or exc}"}
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1816:  # the job's one-process limit
            return {"result": "denied", "detail": "refused by the job object (one process only)"}
        return {
            "result": "error",
            "detail": f"{type(exc).__name__} [{exc.errno}/{getattr(exc, 'winerror', None)}]: "
            f"{exc.strerror or exc}",
        }
    except Exception as exc:
        return {"result": "error", "detail": f"{type(exc).__name__}: {exc}"}


def read_file(path: str) -> str:
    with open(path, "rb") as f:
        f.read(READ_BYTES)
    return "opened and read (content not kept)"


def list_dir(path: str) -> str:
    return f"listed {len(os.listdir(path))} entries (names not kept)"


def write_file(folder: str) -> str:
    path = os.path.join(folder, f"openchart-probe-{os.getpid()}.tmp")
    with open(path, "wb") as f:
        f.write(b"probe")
    os.remove(path)
    return f"created and removed a file in {folder}"


def connect(host: str, port: int, timeout: float) -> str:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return f"connected to {host}:{port}"
    except TimeoutError:
        raise TimeoutError(f"no connection to {host}:{port} within {timeout:g} s") from None
    finally:
        s.close()


def run_program() -> str:
    argv = ["cmd.exe", "/c", "exit 7"] if os.name == "nt" else ["/bin/sh", "-c", "exit 7"]
    r = subprocess.run(argv, timeout=10, capture_output=True)
    return f"a program ran (exit code {r.returncode})"


def open_process(pid: int) -> str:
    if os.name != "nt":
        with open(f"/proc/{pid}/environ", "rb") as f:
            f.read(READ_BYTES)
        return "read its environment (content not kept)"
    import ctypes
    from ctypes import wintypes as w

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = w.HANDLE
    k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    h = k32.OpenProcess(0x0010 | 0x0400, False, pid)  # VM_READ | QUERY_INFORMATION
    if not h:
        err = ctypes.get_last_error()
        if err == 5:
            raise PermissionError(err, "access denied")
        raise OSError(err, ctypes.FormatError(err))
    k32.CloseHandle(h)
    return "opened the server process with memory-read access"


def read_credential(target: str) -> str:
    import ctypes
    from ctypes import wintypes as w

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredReadW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    ptr = ctypes.c_void_p()
    if advapi.CredReadW(target, 1, 0, ctypes.byref(ptr)):
        advapi.CredFree(ptr)
        return "read the credential (content not kept)"
    err = ctypes.get_last_error()
    if err == 5:
        raise PermissionError(err, "access denied")
    if err == 1168:
        raise FileNotFoundError(err, "not found (its own credential partition)")
    raise OSError(err, ctypes.FormatError(err))


def read_registry(subkey: str) -> str:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as k:
        winreg.QueryValueEx(k, "decoy")
    return "read the value (content not kept)"


def environment() -> dict[str, str]:
    names = sorted(os.environ)
    secret = [
        n
        for n in names
        if n.upper() not in NOT_SECRET and any(w in n.upper() for w in SECRET_WORDS)
    ]
    return {
        "result": "secrets visible" if secret else "clean",
        "detail": f"{len(names)} variables"
        + (f"; secret-like: {', '.join(secret)}" if secret else ""),
    }


def identity() -> str:
    if os.name != "nt":
        return f"uid {os.getuid()}, pid {os.getpid()}"
    import ctypes
    from ctypes import wintypes as w

    levels = {0x1000: "Low", 0x2000: "Medium", 0x3000: "High", 0x4000: "System", 0: "Untrusted"}
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = w.HANDLE
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.GetTokenInformation.argtypes = [
        w.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        w.DWORD,
        ctypes.POINTER(w.DWORD),
    ]
    token = w.HANDLE()
    advapi.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(token))
    parts = []
    buf = ctypes.create_string_buffer(256)
    n = w.DWORD()
    if advapi.GetTokenInformation(token, 25, buf, 256, ctypes.byref(n)):  # integrity level
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        count = ctypes.cast(sid + 1, ctypes.POINTER(ctypes.c_ubyte))[0]
        rid = ctypes.cast(sid + 8 + 4 * (count - 1), ctypes.POINTER(ctypes.c_uint32))[0]
        parts.append(f"integrity {levels.get(rid, hex(rid))}")
    if advapi.GetTokenInformation(token, 29, buf, 256, ctypes.byref(n)):  # TokenIsAppContainer
        yes = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint32))[0]
        parts.append(f"appcontainer {'yes' if yes else 'no'}")
    k32.CloseHandle(token)
    return f"pid {os.getpid()}, " + ", ".join(parts)


def run_probes(targets: dict[str, Any]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {
        "identity": {"result": "info", "detail": identity()},
        "environment": environment(),
    }
    for label, path in (targets.get("read") or {}).items():
        out[label] = attempt(lambda p=path: read_file(p))
    for label, path in (targets.get("list") or {}).items():
        out[label] = attempt(lambda p=path: list_dir(p))
    for label, path in (targets.get("write") or {}).items():
        out[label] = attempt(lambda p=path: write_file(p))
    if targets.get("parent_pid"):
        out["open the server process"] = attempt(lambda: open_process(int(targets["parent_pid"])))
    for label, (host, port, timeout) in (targets.get("connect") or {}).items():
        out[label] = attempt(lambda h=host, p=port, t=timeout: connect(h, int(p), float(t)))
    if targets.get("program"):
        out["start a program"] = attempt(run_program)
    # Decoys made by the Windows tests (the server never probes the owner's own entries).
    for label, target in (targets.get("credential") or {}).items():
        out[label] = attempt(lambda t=target: read_credential(t))
    for label, subkey in (targets.get("registry") or {}).items():
        out[label] = attempt(lambda k=subkey: read_registry(k))
    out["python"] = {"result": "info", "detail": sys.version.split()[0]}
    return out
