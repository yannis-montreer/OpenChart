"""Windows: the job object's limits and the worker identities (CI-0 findings, now in the
package).

As the user running the tests: the job's memory, deadline and one-process limits, the
worker dying with its parent, the restricted token (writes and the server process denied;
reads allowed, which is why its notice says "weaker isolation"), and the exit code of a
worker that dies before it is ready.

The AppContainer test needs a Python copy granted to a test container and decoys in
Credential Manager and the registry: it runs where OPENCHART_TEST_APPCONTAINER=1 (CI's
Windows runner), never by default on someone's own PC. LocalSystem is CI's service job
(tools/ci/service-reinstall-test.ps1).
"""

import contextlib
import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import sysconfig
import time
import winreg
from ctypes import wintypes
from pathlib import Path

import numpy as np
import pytest

if os.name != "nt":
    pytest.skip("Windows only", allow_module_level=True)

from openchart.indicators.worker import isolation, windows
from openchart.indicators.worker.isolation import WindowsLauncher, start_worker
from openchart.indicators.worker.runner import IndicatorRunner, Limits, ScriptFailed

SCRIPT = """
from openchart.indicators import Color, Output, Width, sma
NAME = "S"
OVERLAY = True
PARAMS = [Color("color", "#2962ff"), Width("width", 1)]
OUTPUTS = [Output("out", "Out", color="color", width="width")]
def compute(bars, params):
{body}
    return {{"out": sma(bars["c"], 2)}}
"""
BARS = {k: np.arange(10, dtype="f8") for k in ("ts", "o", "h", "l", "c", "v")}


def script(body: str = "    pass"):
    text = SCRIPT.format(body=body)
    return hashlib.sha256(text.encode()).hexdigest(), "s.py", text


async def restricted(timeout_s=1.0, memory_mb=512):
    limits = Limits(timeout_s=timeout_s, memory_mb=memory_mb)
    runner = IndicatorRunner(WindowsLauncher("restricted", limits), limits)
    await runner.start()
    return runner


async def test_the_memory_limit_raises_memory_error_and_the_worker_keeps_answering():
    runner = await restricted(timeout_s=10, memory_mb=512)
    try:
        with pytest.raises(ScriptFailed) as failed:
            await runner.compute(*script("    x = bytearray(2 * 2**30)"), {}, BARS)
        assert failed.value.kind == "memory"
        assert (await runner.compute(*script(), {}, BARS))["out"].size == 10
        assert runner.restarts == 0
    finally:
        await runner.stop()


@pytest.mark.parametrize(
    "body", ["    while True:\n        pass", "    import time\n    time.sleep(3600)"]
)
async def test_a_hang_or_a_sleep_is_killed_at_the_deadline(body):
    runner = await restricted(timeout_s=1.0)
    try:
        t = time.perf_counter()
        with pytest.raises(ScriptFailed) as failed:
            await runner.compute(*script(body), {}, BARS)
        assert failed.value.kind == "timeout" and time.perf_counter() - t < 2
        assert (await runner.compute(*script(), {}, BARS))["out"].size == 10
    finally:
        await runner.stop()


async def test_restricted_probes(tmp_path):
    """Below the minimum, and the notice must stay honest: reads are allowed."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "openchart.db").write_bytes(b"decoy")
    writable = tmp_path / "writable"
    writable.mkdir()
    limits = Limits()
    runner, iso = await start_worker(data, limits, [WindowsLauncher("restricted", limits)])
    try:
        p = iso.probes
        assert iso.level == "restricted" and not iso.minimum_met
        assert p["environment"]["result"] == "clean"
        assert p["identity"]["detail"].endswith("integrity Low, appcontainer no")
        assert p["read the database"]["result"] == "allowed"  # why it is "weaker isolation"
        assert p["list the data folder"]["result"] == "allowed"
        assert p["open the server process"]["result"] == "denied"
        assert p["start a program"]["result"] == "denied"
    finally:
        await runner.stop()
    # Writes, on a fresh worker (probes run only before it is armed).
    fresh = IndicatorRunner(WindowsLauncher("restricted", limits), limits)
    started = await fresh.start({"write": {"write a folder of the user": str(writable)}})
    try:
        assert started["probes"]["write a folder of the user"]["result"] == "denied"
    finally:
        await fresh.stop()


async def test_the_audit_hook_refuses_files_and_sockets_with_a_message():
    runner = await restricted(timeout_s=5)
    try:
        for body, words in (
            ("    open(r'C:\\Windows\\win.ini').read()", "cannot open files"),
            (
                "    import socket\n    socket.socket().connect(('127.0.0.1', 9))",
                "cannot use socket",
            ),
            ("    import subprocess\n    subprocess.run(['cmd'])", "cannot use subprocess"),
        ):
            with pytest.raises(ScriptFailed) as failed:
                await runner.compute(*script(body), {}, BARS)
            assert words in str(failed.value)
    finally:
        await runner.stop()


def test_the_worker_dies_with_the_server(tmp_path):
    """The job is killed when its handle closes: kill the parent without any cleanup."""
    code = (
        "import asyncio, sys\n"
        "from openchart.indicators.worker.isolation import WindowsLauncher\n"
        "from openchart.indicators.worker.runner import IndicatorRunner, Limits\n"
        "async def main():\n"
        "    r = IndicatorRunner(WindowsLauncher('restricted', Limits()), Limits())\n"
        "    out = await r.start()\n"
        "    print(out['hello']['pid'], flush=True)\n"
        "    await asyncio.sleep(3600)\n"
        "asyncio.run(main())\n"
    )
    env = {**os.environ, "PYTHONPATH": isolation.package_root()}
    parent = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=env
    )
    try:
        pid = int(parent.stdout.readline())
        assert pid_alive(pid)
    finally:
        parent.kill()  # TerminateProcess: nothing runs in the parent
        parent.wait(10)
    end = time.monotonic() + 5
    while pid_alive(pid) and time.monotonic() < end:
        time.sleep(0.1)
    assert not pid_alive(pid)


def pid_alive(pid: int) -> bool:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    code = wintypes.DWORD()
    k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return code.value == 259


async def test_a_worker_that_dies_before_it_is_ready_reports_its_exit_code(tmp_path):
    limits = Limits()
    argv = [
        isolation.base_python(),
        "-I",
        "-c",
        "import sys; sys.stderr.write('no luck'); sys.exit(7)",
    ]
    launcher = WindowsLauncher("restricted", limits, argv=argv, cwd=str(Path(argv[0]).parent))
    runner, iso = await start_worker(tmp_path, limits, [launcher])
    assert runner is None and iso.level == "unavailable"
    assert "exit code 0x00000007" in iso.reason and "no luck" in iso.reason


def test_the_container_sid_depends_on_the_name_only():
    sid = windows.container_sid()
    assert sid.startswith("S-1-15-2-") and sid == windows.container_sid()
    assert windows.container_sid("Another.Name") != sid


# AppContainer as the user (CI) ------------------------------------------------------------

APPCONTAINER = os.environ.get("OPENCHART_TEST_APPCONTAINER") == "1"
TEST_CONTAINER = "OpenChart.IndicatorWorker.Test"
CREDENTIAL = "OpenChartTest/decoy"
REGISTRY = r"Software\OpenChartTestDecoy"


def copy_runtime(dst: Path) -> list[str]:
    """A copy of the interpreter and the packages the worker imports, which the test
    container may read (never the real Python's folders)."""
    base = Path(sys.base_prefix)
    py = dst / "python"
    skip = shutil.ignore_patterns(
        "site-packages",
        "test",
        "tests",
        "idlelib",
        "tkinter",
        "turtledemo",
        "ensurepip",
        "__pycache__",
    )
    shutil.copytree(base / "Lib", py / "Lib", ignore=skip)
    shutil.copytree(
        base / "DLLs", py / "DLLs", ignore=shutil.ignore_patterns("tcl*", "tk*", "_tkinter*")
    )
    for f in base.iterdir():
        if f.is_file() and f.suffix.lower() in (".exe", ".dll"):
            shutil.copy2(f, py / f.name)
    sp = dst / "site-packages"
    src_sp = Path(sysconfig.get_paths()["purelib"])
    for name in (
        "numpy",
        "numpy.libs",
        "pandas",
        "pandas.libs",
        "dateutil",
        "pytz",
        "tzdata",
        "six.py",
    ):
        if (src_sp / name).is_dir():
            shutil.copytree(
                src_sp / name, sp / name, ignore=shutil.ignore_patterns("tests", "__pycache__")
            )
        elif (src_sp / name).is_file():
            shutil.copy2(src_sp / name, sp / name)
    shutil.copytree(
        Path(isolation.package_root()) / "openchart",
        dst / "src" / "openchart",
        ignore=shutil.ignore_patterns("web", "__pycache__"),
    )
    return [str(py / "python.exe"), str(dst / "src"), str(sp)]


@pytest.mark.skipif(not APPCONTAINER, reason="set OPENCHART_TEST_APPCONTAINER=1 (CI)")
async def test_appcontainer_probes_against_decoys(tmp_path):
    from openchart.indicators.worker import child

    python, src, sp = copy_runtime(tmp_path / "rt")
    sid = windows.ensure_profile(TEST_CONTAINER)
    sid_text = windows.sid_to_string(sid)
    r = subprocess.run(  # noqa: ASYNC221 (a test: nothing else runs meanwhile)
        ["icacls", str(tmp_path / "rt"), "/grant", f"*{sid_text}:(OI)(CI)RX", "/T", "/C", "/Q"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    data = tmp_path / "data"
    data.mkdir()
    (data / "openchart.db").write_bytes(b"decoy")
    (tmp_path / ".env").write_text("DECOY=1\n")
    write_credential(CREDENTIAL)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY) as k:
        winreg.SetValueEx(k, "decoy", 0, winreg.REG_SZ, "decoy")
    try:
        child_py = Path(src) / "openchart" / "indicators" / "worker" / Path(child.__file__).name
        argv = [python, "-I", "-B", "-X", "utf8", str(child_py), "--path", src, "--site", sp]
        limits = Limits(timeout_s=10)
        launcher = WindowsLauncher(
            "appcontainer",
            limits,
            argv=argv,
            cwd=str(Path(python).parent),
            container_name=TEST_CONTAINER,
        )
        os.environ["OPENCHART_SECRET_KEY_FILE"] = str(tmp_path / ".env")  # a key file to probe
        try:
            fresh = IndicatorRunner(launcher, limits)
            targets = isolation.probe_targets(data, 9, "appcontainer")
            targets["read"]["read a .env in a user folder"] = str(tmp_path / ".env")
            targets["list"]["list the user profile"] = os.environ["USERPROFILE"]
            targets["credential"] = {"Credential Manager decoy": CREDENTIAL}
            targets["registry"] = {"registry decoy (HKCU)": REGISTRY}
            targets["write"] = {"write into the data folder": str(data)}
            started = await fresh.start(targets)
        finally:
            del os.environ["OPENCHART_SECRET_KEY_FILE"]
        try:
            probes = started["probes"]
            met, why = isolation.judge("appcontainer", probes)
            assert met, (why, probes)
            assert probes["identity"]["detail"].endswith("appcontainer yes")
            for name in (
                "read a .env in a user folder",
                "list the user profile",
                "Credential Manager decoy",
                "registry decoy (HKCU)",
                "write into the data folder",
            ):
                assert probes[name]["result"] != "allowed", (name, probes[name])
            assert (await fresh.compute(*script(), {}, BARS))["out"].size == 10
        finally:
            await fresh.stop()
    finally:
        delete_credential(CREDENTIAL)
        with contextlib.suppress(OSError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REGISTRY)
        windows.delete_profile(TEST_CONTAINER)


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.c_void_p),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def write_credential(target: str) -> None:
    blob = b"decoy"
    buf = ctypes.create_string_buffer(blob)
    cred = CREDENTIALW(
        Type=1,
        TargetName=target,
        CredentialBlobSize=len(blob),
        CredentialBlob=ctypes.cast(buf, ctypes.c_void_p),
        Persist=1,
        UserName="decoy",
    )
    assert ctypes.WinDLL("advapi32").CredWriteW(ctypes.byref(cred), 0)


def delete_credential(target: str) -> None:
    advapi = ctypes.WinDLL("advapi32")
    advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi.CredDeleteW(target, 1, 0)


async def test_a_release_zip_run_by_hand_gets_the_weaker_isolation_notice(monkeypatch, tmp_path):
    """Owner's decision 1: not the service, so the restricted token at the build's own
    integrity (PyInstaller cannot start at Low), below the minimum, with the notice."""
    monkeypatch.setattr(isolation, "frozen", lambda: True)
    monkeypatch.setattr(windows, "running_as_system", lambda: False)
    launchers = isolation.plan(Limits()).launchers
    assert [(x.identity, x.low_integrity) for x in launchers] == [
        ("restricted", False),
        ("plain", True),
    ]
    # Started here on the source interpreter, at that same integrity.
    monkeypatch.setattr(isolation, "frozen", lambda: False)
    limits = Limits()
    launcher = WindowsLauncher("restricted", limits, low_integrity=False)
    runner, iso = await start_worker(tmp_path, limits, [launcher])
    try:
        assert iso.level == "restricted" and not iso.minimum_met
        assert iso.notice == isolation.WEAKER_NOTICE
        assert iso.to_json()["notice"].startswith("weaker isolation")
    finally:
        await runner.stop()
