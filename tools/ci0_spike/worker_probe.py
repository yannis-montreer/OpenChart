"""CI-0 spike, the worker side: what a custom-indicator script would run inside.

It talks to its parent over stdin/stdout in frames: a 4-byte little-endian length, a JSON
header, then the raw float64 arrays the header lists. Nothing else goes to stdout.

Commands: `probe` (what can this process reach?), `compute` (a typical script on the bars
sent), `hang`, `sleep`, `alloc`, `cpu`, `exit`. Probes report what happened (allowed,
denied, the error), never the content of anything they managed to read.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time

T0 = time.perf_counter()

if "--site" in sys.argv:  # a source install: the venv's packages, without its launcher
    import site

    site.addsitedir(sys.argv[sys.argv.index("--site") + 1])

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

IMPORT_S = time.perf_counter() - T0


# Frames ------------------------------------------------------------------------------


def read_exact(f, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        chunk = f.read(n - len(out))
        if not chunk:
            raise EOFError
        out += chunk
    return bytes(out)


def recv(f) -> tuple[dict, dict[str, np.ndarray]]:
    (size,) = struct.unpack("<I", read_exact(f, 4))
    header = json.loads(read_exact(f, size))
    arrays = {}
    for name, n in header.get("arrays", []):
        arrays[name] = np.frombuffer(read_exact(f, 8 * n), dtype="<f8")
    return header, arrays


def send(f, header: dict, arrays: dict[str, np.ndarray] | None = None) -> None:
    arrays = arrays or {}
    header = dict(header, arrays=[[k, len(v)] for k, v in arrays.items()])
    body = json.dumps(header).encode()
    parts = [struct.pack("<I", len(body)), body]
    parts += [np.ascontiguousarray(v, dtype="<f8").tobytes() for v in arrays.values()]
    f.write(b"".join(parts))
    f.flush()


# A typical script: EMA, SMA, RSI, Bollinger band, a cross ------------------------------


def ema(x: pd.Series, n: int) -> pd.Series:
    out = pd.Series(np.nan, index=x.index)
    if len(x) < n:
        return out
    tail = x.iloc[n - 1 :].copy()
    tail.iloc[0] = x.iloc[:n].mean()
    out.iloc[n - 1 :] = tail.ewm(alpha=2 / (n + 1), adjust=False).mean().to_numpy()
    return out


def rma(x: pd.Series, n: int) -> pd.Series:
    out = pd.Series(np.nan, index=x.index)
    tail = x.iloc[n - 1 :].copy()
    tail.iloc[0] = x.iloc[:n].mean()
    out.iloc[n - 1 :] = tail.ewm(alpha=1 / n, adjust=False).mean().to_numpy()
    return out


def typical(arrays: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    close = pd.Series(arrays["c"])
    fast, slow = ema(close, 9), close.rolling(20).mean()
    change = close.diff()
    up, down = rma(change.clip(lower=0).fillna(0), 14), rma((-change).clip(lower=0).fillna(0), 14)
    rsi = 100 - 100 / (1 + up / down)
    dev = close.rolling(20).std(ddof=0)
    cross = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    return {
        "fast": fast.to_numpy(),
        "slow": slow.to_numpy(),
        "rsi": rsi.to_numpy(),
        "upper": (slow + 2 * dev).to_numpy(),
        "lower": (slow - 2 * dev).to_numpy(),
        "cross": cross.astype("float64").to_numpy(),
    }


# Probes -------------------------------------------------------------------------------


def attempt(fn) -> dict:
    try:
        return {"result": "allowed", "detail": fn()}
    except TimeoutError as exc:
        return {"result": "timed out", "detail": str(exc)}
    except PermissionError as exc:
        return {"result": "denied", "detail": f"{type(exc).__name__}: {exc.strerror or exc}"}
    except OSError as exc:
        return {
            "result": "error",
            "detail": f"{type(exc).__name__} [{exc.errno}/{getattr(exc, 'winerror', None)}]: "
            f"{exc.strerror or exc}",
        }
    except Exception as exc:
        return {"result": "error", "detail": f"{type(exc).__name__}: {exc}"}


def read_file(path: str) -> str:
    with open(path, "rb") as f:
        return f"read {len(f.read())} bytes (content not shown)"


def list_dir(path: str) -> str:
    return f"listed {len(os.listdir(path))} entries (names not shown)"


def write_file(folder: str) -> str:
    path = os.path.join(folder, "ci0-probe-write.tmp")
    with open(path, "wb") as f:
        f.write(b"probe")
    os.remove(path)
    return f"created and removed a file in {folder}"


def cred_read(target: str) -> str:
    import ctypes
    from ctypes import wintypes as w

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    ptr = ctypes.c_void_p()
    advapi.CredReadW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    if advapi.CredReadW(target, 1, 0, ctypes.byref(ptr)):
        advapi.CredFree(ptr)
        return "credential read (content not shown)"
    err = ctypes.get_last_error()
    if err == 5:
        raise PermissionError(err, "access denied")
    raise OSError(err, {1168: "not found (ERROR_NOT_FOUND)"}.get(err, ctypes.FormatError(err)))


def open_process(pid: int) -> str:
    import ctypes
    from ctypes import wintypes as w

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = w.HANDLE
    h = k32.OpenProcess(0x0010 | 0x0400, False, pid)  # VM_READ | QUERY_INFORMATION
    if not h:
        err = ctypes.get_last_error()
        if err == 5:
            raise PermissionError(err, "access denied")
        raise OSError(err, ctypes.FormatError(err))
    k32.CloseHandle(h)
    return "opened the parent with memory-read access"


def registry(subkey: str) -> str:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as k:
        winreg.QueryValueEx(k, "decoy")
    return "read the decoy value (content not shown)"


def connect(host: str, port: int) -> str:
    import socket

    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((host, port))
        return f"connected to {host}:{port}"
    except TimeoutError:
        raise TimeoutError(f"no connection to {host}:{port} within 3 s") from None
    finally:
        s.close()


def run_program() -> str:
    import subprocess

    r = subprocess.run(["cmd.exe", "/c", "exit 7"], timeout=10, capture_output=True)
    return f"cmd.exe ran, exit code {r.returncode}"


def probes(req: dict) -> dict:
    d = req["decoys"]
    names = sorted(os.environ)
    secret_like = [n for n in names if n.upper().startswith(("OPENCHART", "OANDA", "TELEGRAM"))]
    out = {
        "identity": {"result": "info", "detail": identity()},
        "environment": {
            "result": "secrets visible" if secret_like else "clean",
            "detail": f"{len(names)} variables: {', '.join(names)}; "
            f"secret-like: {secret_like or 'none'}",
        },
        "read decoy .env (user folder)": attempt(lambda: read_file(d["env"])),
        "read decoy database": attempt(lambda: read_file(d["db"])),
        "read decoy key file": attempt(lambda: read_file(d["key"])),
        "list decoy data folder": attempt(lambda: list_dir(d["data"])),
        "list the user profile": attempt(lambda: list_dir(d["profile"])),
        "write into the decoy data folder": attempt(lambda: write_file(d["data"])),
        "write into the working folder": attempt(lambda: write_file(os.getcwd())),
        "write into its own TEMP": attempt(
            lambda: write_file(os.environ.get("TEMP", "C:\\nonexistent"))
        ),
        "write into Windows\\Temp": attempt(
            lambda: write_file(os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "Temp"))
        ),
        "Credential Manager decoy": attempt(lambda: cred_read(d["credential"])),
        "registry decoy (HKCU)": attempt(lambda: registry(d["registry"])),
        "open the parent process": attempt(lambda: open_process(req["parent_pid"])),
        "connect to the server port on 127.0.0.1": attempt(
            lambda: connect("127.0.0.1", req["port"])
        ),
        "connect out (192.0.2.1:9, a reserved test address)": attempt(
            lambda: connect("192.0.2.1", 9)
        ),
        "start a program": attempt(run_program),
    }
    return out


LEVELS = {0x1000: "Low", 0x2000: "Medium", 0x3000: "High", 0x4000: "System", 0: "Untrusted"}


def identity() -> str:
    import ctypes
    from ctypes import wintypes as w

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = w.HANDLE
    token = w.HANDLE()
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(token))
    advapi.GetTokenInformation.argtypes = [
        w.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        w.DWORD,
        ctypes.POINTER(w.DWORD),
    ]
    parts = []
    U32 = ctypes.POINTER(ctypes.c_uint32)
    for cls, _label in ((25, "integrity"), (29, "appcontainer")):
        buf = ctypes.create_string_buffer(256)
        n = w.DWORD()
        if advapi.GetTokenInformation(token, cls, buf, 256, ctypes.byref(n)):
            if cls == 25:
                sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
                count = ctypes.cast(sid + 1, ctypes.POINTER(ctypes.c_ubyte))[0]
                rid = ctypes.cast(sid + 8 + 4 * (count - 1), ctypes.POINTER(ctypes.c_uint32))[0]
                parts.append(f"integrity {LEVELS.get(rid, hex(rid))}")
            else:
                parts.append(f"appcontainer {'yes' if ctypes.cast(buf, U32)[0] else 'no'}")
    return f"pid {os.getpid()}, " + ", ".join(parts)


# Main loop ----------------------------------------------------------------------------


def main() -> None:
    fin, fout = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr  # nothing but frames on the real stdout
    send(
        fout,
        {
            "ready": True,
            "import_s": IMPORT_S,
            "python": sys.version.split()[0],
            "frozen": bool(getattr(sys, "frozen", False)),
        },
    )
    while True:
        try:
            req, arrays = recv(fin)
        except EOFError:
            return
        cmd = req["cmd"]
        if cmd == "exit":
            return
        if cmd == "probe":
            send(fout, {"probes": probes(req)})
        elif cmd == "compute":
            t = time.perf_counter()
            out = typical(arrays)
            send(fout, {"compute_ms": (time.perf_counter() - t) * 1000}, out)
        elif cmd == "hang":
            while True:
                pass
        elif cmd == "sleep":
            time.sleep(3600)
        elif cmd == "cpu":
            end = time.perf_counter() + req["seconds"]
            while time.perf_counter() < end:
                pass
            send(fout, {"done": True})
        elif cmd == "alloc":
            held, mb = [], 0
            try:
                while mb < req["mb"]:
                    held.append(bytearray(64 * 2**20))
                    mb += 64
                send(fout, {"alloc": f"allocated {mb} MB"})
            except MemoryError:
                del held
                send(fout, {"alloc": f"MemoryError after {mb} MB"})


if __name__ == "__main__":
    main()
