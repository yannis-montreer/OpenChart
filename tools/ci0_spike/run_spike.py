"""CI-0 spike driver: start the worker in each isolation mode, probe it, measure it.

    python run_spike.py --work <folder> --mode <name>=<identity>:<runtime kind>:<path> ...
        [--decoy-data <folder>] [--decoy-key <file>] [--report <file.json>]

Identities: appcontainer, restricted, plain. Runtime kinds:
  cpython:<folder>  a copy of Python with numpy and pandas (python.exe in it)
  frozen:<exe>      the PyInstaller build of worker_probe.py
  venv:<site-packages>  the base Python of this interpreter plus a venv's packages (a
                    source install: no copy, no ACL change)

Everything the probes aim at is a decoy made here: a `.env`, a database, a key file, a
Credential Manager entry and a registry value, all removed at the end. Containers grant
read and execute only on runtime folders under --work (copies made for the test).
"""

from __future__ import annotations

import argparse
import ctypes as C
import json
import os
import queue
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import winreg
from ctypes import wintypes as W
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import winsandbox as sb  # noqa: E402
from worker_probe import recv, send  # noqa: E402

CONTAINER = "OpenChart.IndicatorWorker.Spike"
CREDENTIAL = "OpenChartSpike/decoy"
REGISTRY = r"Software\OpenChartSpike"
ADMINS, SYSTEM = "S-1-5-32-544", "S-1-5-18"


# Decoys -------------------------------------------------------------------------------


class CREDENTIALW(C.Structure):
    _fields_ = [
        ("Flags", W.DWORD),
        ("Type", W.DWORD),
        ("TargetName", W.LPWSTR),
        ("Comment", W.LPWSTR),
        ("LastWritten", W.FILETIME),
        ("CredentialBlobSize", W.DWORD),
        ("CredentialBlob", C.c_void_p),
        ("Persist", W.DWORD),
        ("AttributeCount", W.DWORD),
        ("Attributes", C.c_void_p),
        ("TargetAlias", W.LPWSTR),
        ("UserName", W.LPWSTR),
    ]


def make_decoys(work: Path, data: Path | None, key: Path | None) -> dict:
    root = work / "decoys"
    data = data or root / "data"
    root.mkdir(parents=True, exist_ok=True)  # also when the data folder is given elsewhere
    data.mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text("DECOY_TOKEN=this-is-not-a-real-token\n")
    (data / "openchart.db").write_bytes(os.urandom(4096))
    key = key or root / "secret.key"
    if not key.exists():
        key.write_text("decoy-key\n")
    blob = b"decoy-secret"
    cred = CREDENTIALW(
        Type=1,
        TargetName=CREDENTIAL,
        CredentialBlobSize=len(blob),
        CredentialBlob=C.cast(C.create_string_buffer(blob), C.c_void_p),
        Persist=1,
        UserName="decoy",
    )
    adv = C.WinDLL("advapi32", use_last_error=True)
    if not adv.CredWriteW(C.byref(cred), 0):
        raise OSError(C.get_last_error(), "CredWrite")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY) as k:
        winreg.SetValueEx(k, "decoy", 0, winreg.REG_SZ, "decoy-secret")
    return {
        "env": str(root / ".env"),
        "db": str(data / "openchart.db"),
        "key": str(key),
        "data": str(data),
        "profile": os.environ.get("USERPROFILE", str(Path.home())),
        "credential": CREDENTIAL,
        "registry": REGISTRY,
    }


def remove_decoys(work: Path) -> list[str]:
    done = []
    C.windll.advapi32.CredDeleteW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD]
    deleted = bool(C.windll.advapi32.CredDeleteW(CREDENTIAL, 1, 0))
    done.append(f"credential {CREDENTIAL} deleted now: {deleted} (False: it was not there)")
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REGISTRY)
        done.append(f"registry key HKCU\\{REGISTRY} deleted")
    except FileNotFoundError:
        done.append("registry key already gone")
    shutil.rmtree(work / "decoys", ignore_errors=True)
    return done


# Talking to a worker ------------------------------------------------------------------


class Channel:
    def __init__(self, worker: sb.Worker):
        self.w = worker
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                self.q.put(recv(self.w.stdout))
        except Exception as exc:  # EOF when it dies
            self.q.put(exc)

    def ask(self, header: dict, arrays=None, timeout: float = 30):
        send(self.w.stdin, header, arrays)
        return self.get(timeout)

    def get(self, timeout: float):
        got = self.q.get(timeout=timeout)
        if isinstance(got, Exception):
            raise got
        return got


def runtime_argv(kind: str, path: str) -> tuple[list[str], str]:
    probe = HERE / "worker_probe.py"
    if kind == "cpython":
        dst = Path(path) / "worker_probe.py"
        shutil.copyfile(probe, dst)
        return [str(Path(path) / "python.exe"), "-I", "-B", "-X", "utf8", str(dst)], path
    if kind == "frozen":
        return [path], str(Path(path).parent)
    if kind == "venv":
        base = Path(sys.base_prefix) / "python.exe"
        return [str(base), "-I", "-B", "-X", "utf8", str(probe), "--site", path], str(HERE)
    raise ValueError(kind)


def clean_env(runtime_dir: str) -> dict[str, str]:
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return {
        "SYSTEMROOT": root,
        "PATH": f"{runtime_dir};{root}\\System32",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }


def launch(identity: str, argv: list[str], cwd: str, work: Path, sid, env_mode: str):
    env = None if env_mode == "inherit" else clean_env(str(Path(argv[0]).parent))
    if env is not None and identity == "appcontainer":
        # CreateProcess fails with error 203 without it (it maps the container's own folder)
        env["LOCALAPPDATA"] = os.environ.get("LOCALAPPDATA") or os.path.expandvars(
            r"%SystemRoot%\System32\config\systemprofile\AppData\Local"
        )
    t = time.perf_counter()
    w = sb.start(
        argv,
        identity=identity,
        env=env,
        cwd=cwd,
        stderr_path=str(work / f"worker-{identity}.err"),
        container_sid=sid,
        disable_sids=[ADMINS, SYSTEM],
    )
    ch = Channel(w)
    hello, _ = ch.get(60)
    return w, ch, (time.perf_counter() - t), hello


def bars(n: int) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(7)
    c = 2000 + np.cumsum(rng.normal(0, 1, n))
    o = np.r_[c[0], c[:-1]]
    return {
        "ts": np.arange(n, dtype="f8") * 60,
        "o": o,
        "h": np.maximum(o, c) + 0.5,
        "l": np.minimum(o, c) - 0.5,
        "c": c,
        "v": rng.integers(1, 100, n).astype("f8"),
    }


def kill_time(w: sb.Worker) -> float:
    t = time.perf_counter()
    w.kill()
    w.wait(10_000)
    return (time.perf_counter() - t) * 1000


def run_mode(
    name: str, identity: str, kind: str, path: str, work: Path, decoys: dict, sid, port: int
) -> dict:
    argv, cwd = runtime_argv(kind, path)
    env_mode = "inherit" if identity == "plain" else "clean"
    out: dict = {"mode": name, "identity": identity, "runtime": kind, "env": env_mode}
    try:
        w, ch, start_s, hello = launch(identity, argv, cwd, work, sid, env_mode)
    except Exception as exc:
        out["start"] = f"FAILED: {type(exc).__name__}: {exc}"
        err = work / f"worker-{identity}.err"
        if err.exists():
            out["stderr"] = err.read_text(errors="replace")[-2000:]
        return out
    out["start_s"] = round(start_s, 3)
    out["import_s"] = round(hello["import_s"], 3)
    out["python"] = hello["python"] + (" (frozen)" if hello["frozen"] else "")

    probe, _ = ch.ask(
        {"cmd": "probe", "decoys": decoys, "parent_pid": os.getpid(), "port": port}, timeout=60
    )
    out["probes"] = probe["probes"]

    b = bars(5000 + 200)
    rt, inner = [], []
    for _ in range(20):
        t = time.perf_counter()
        head, arrays = ch.ask({"cmd": "compute"}, b)
        rt.append((time.perf_counter() - t) * 1000)
        inner.append(head["compute_ms"])
    out["compute_5200_bars_ms"] = {
        "worker_median": round(statistics.median(inner), 1),
        "round_trip_median": round(statistics.median(rt), 1),
        "round_trip_max": round(max(rt), 1),
    }
    assert len(arrays["fast"]) == 5200

    cpu0, t = sb.job_cpu_seconds(w.job), time.perf_counter()
    ch.ask({"cmd": "cpu", "seconds": 4}, timeout=30)
    wall = time.perf_counter() - t
    out["cpu_cap"] = {
        "cap": "50% of one core",
        "measured": f"{(sb.job_cpu_seconds(w.job) - cpu0) / wall * 100:.0f}% of one core "
        f"over {wall:.1f} s",
    }

    head, _ = ch.ask({"cmd": "alloc", "mb": 2048}, timeout=60)
    out["memory"] = {
        "limit": "512 MB",
        "result": head["alloc"],
        "peak_mb": round(sb.job_peak_mb(w.job)),
        "worker_still_answers": bool(ch.ask({"cmd": "compute"}, bars(300))[0]),
    }

    for cmd in ("hang", "sleep"):
        send(w.stdin, {"cmd": cmd})
        try:
            ch.get(timeout=1.0)
            out[f"timeout_{cmd}"] = "UNEXPECTED: it answered"
        except queue.Empty:
            ms = kill_time(w)
            out[f"timeout_{cmd}"] = (
                f"deadline 1 s passed; killed in {ms:.0f} ms (exit code {w.exit_code()})"
            )
        w.close()
        t = time.perf_counter()
        w, ch, start_s, hello = launch(identity, argv, cwd, work, sid, env_mode)
        out[f"restart_after_{cmd}_s"] = round(time.perf_counter() - t, 3)

    ch.ask({"cmd": "probe", "decoys": decoys, "parent_pid": os.getpid(), "port": port}, timeout=60)
    send(w.stdin, {"cmd": "exit"})
    w.wait(5000)
    w.close()
    return out


def orphan_test(mode_arg: str, work: Path) -> str:
    """The parent dies without cleaning up: the job must take the worker with it."""
    child = subprocess.Popen(
        [sys.executable, __file__, "--work", str(work), "--orphan-child", mode_arg],
        stdout=subprocess.PIPE,
        text=True,
    )
    line = child.stdout.readline().strip()
    if not line.startswith("worker pid "):
        child.kill()
        return f"FAILED to start: {line!r}"
    pid = int(line.split()[-1])
    alive_before = sb.pid_alive(pid)
    child.kill()  # TerminateProcess: no clean-up runs in the parent
    child.wait()
    time.sleep(1)
    after = sb.pid_alive(pid)
    return f"worker {pid} alive before: {alive_before}; parent killed; alive 1 s later: {after}"


def orphan_child(mode_arg: str, work: Path) -> None:
    _name, rest = mode_arg.split("=", 1)
    identity, kind, path = rest.split(":", 2)
    sid = sb.appcontainer_sid(CONTAINER) if identity == "appcontainer" else None
    argv, cwd = runtime_argv(kind, path)
    w, _ch, *_ = launch(identity, argv, cwd, work, sid, "clean")
    print(f"worker pid {w.pid}", flush=True)
    time.sleep(3600)


# Main ---------------------------------------------------------------------------------


def listener() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()

    def accept():
        while True:
            conn, _ = s.accept()
            conn.close()

    threading.Thread(target=accept, daemon=True).start()
    return s.getsockname()[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--mode", action="append", default=[])
    ap.add_argument(
        "--grant", action="append", default=[], help="runtime folder to grant to the container"
    )
    ap.add_argument("--decoy-data")
    ap.add_argument("--decoy-key")
    ap.add_argument("--report")
    ap.add_argument("--orphan-child")
    ap.add_argument("--keep-container", action="store_true")
    ap.add_argument("--cleanup-only", action="store_true", help="remove decoys and the profile")
    a = ap.parse_args()
    work = Path(a.work)
    if a.orphan_child:
        return orphan_child(a.orphan_child, work)
    if a.cleanup_only:  # idempotent: also after a run that died half way
        done = remove_decoys(work)
        hr = sb.delete_appcontainer(CONTAINER)
        done.append(
            f"AppContainer profile {CONTAINER} deleted or absent: HRESULT {hr & 0xFFFFFFFF:#x}"
        )
        print(json.dumps(done, indent=1), flush=True)
        return None

    os.environ["OPENCHART_SECRET_KEY"] = "decoy-not-a-key"  # the "plain" mode shows it leaking
    report: dict = {
        "machine": {
            "cpus": os.cpu_count(),
            "user": os.environ.get("USERNAME"),
            "python": sys.version.split()[0],
        },
        "modes": [],
        "cleanup": [],
    }
    sid = None
    decoys = make_decoys(
        work,
        Path(a.decoy_data) if a.decoy_data else None,
        Path(a.decoy_key) if a.decoy_key else None,
    )
    try:
        if any("=appcontainer:" in m for m in a.mode):
            sid = sb.appcontainer_sid(CONTAINER)
            report["container_sid"] = sb.sid_to_string(sid)
            for folder in a.grant:
                if not Path(folder).resolve().is_relative_to(work.resolve()):
                    raise SystemExit(f"refusing to change ACLs outside {work}: {folder}")
                sb.grant_folder(folder, report["container_sid"])
        port = listener()
        for m in a.mode:
            name, rest = m.split("=", 1)
            identity, kind, path = rest.split(":", 2)
            print(f"--- {name}", flush=True)
            result = run_mode(name, identity, kind, path, work, decoys, sid, port)
            if "start_s" in result:
                result["parent_dies"] = orphan_test(m, work)
            report["modes"].append(result)
            print(json.dumps(result, indent=1), flush=True)
    finally:
        report["cleanup"] += remove_decoys(work)
        if sid is not None and not a.keep_container:
            hr = sb.delete_appcontainer(CONTAINER)
            report["cleanup"].append(
                f"AppContainer profile {CONTAINER} deleted: HRESULT {hr & 0xFFFFFFFF:#x}"
            )
        if a.report:
            Path(a.report).write_text(json.dumps(report, indent=1))
        print(json.dumps(report["cleanup"], indent=1))


if __name__ == "__main__":
    main()
