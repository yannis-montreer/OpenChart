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

import os

import pytest

if os.name != "nt":  # before the Windows-only modules below (winreg)
    pytest.skip("Windows only", allow_module_level=True)

import asyncio
import contextlib
import ctypes
import hashlib
import shutil
import socket
import subprocess
import sys
import sysconfig
import time
import winreg
from ctypes import wintypes
from pathlib import Path

import numpy as np

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


def _restricted_blocked() -> str | None:
    """Owner's decision A1: the restricted-token tests skip only where a restricted worker
    was seen to die at start with exactly STATUS_DLL_INIT_FAILED (GitHub's runner, where no
    property of the session predicts it). Any other failure is not a reason to skip: those
    tests then fail and show it."""

    async def attempt() -> str | None:
        limits = Limits()
        runner = IndicatorRunner(WindowsLauncher("restricted", limits), limits)
        try:
            await runner.start()
        except Exception as exc:
            text = str(exc)
            return text if isolation.RESTRICTED_FALLBACK in text else None
        await runner.stop()
        return None

    return asyncio.run(attempt())


RESTRICTED_BLOCKED = _restricted_blocked()
# The job object's limits and the audit hook are the same for the plain worker, so those
# tests run with it where the restricted one cannot start.
JOB_IDENTITY = "plain" if RESTRICTED_BLOCKED else "restricted"
interactive_only = pytest.mark.skipif(
    bool(RESTRICTED_BLOCKED),
    reason=f"the restricted worker cannot start here: {RESTRICTED_BLOCKED}",
)


async def job_worker(timeout_s=1.0, memory_mb=512):
    limits = Limits(timeout_s=timeout_s, memory_mb=memory_mb)
    runner = IndicatorRunner(WindowsLauncher(JOB_IDENTITY, limits), limits)
    await runner.start()
    return runner


async def test_the_memory_limit_raises_memory_error_and_the_worker_keeps_answering():
    runner = await job_worker(timeout_s=10, memory_mb=512)
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
    runner = await job_worker(timeout_s=1.0)
    try:
        t = time.perf_counter()
        with pytest.raises(ScriptFailed) as failed:
            await runner.compute(*script(body), {}, BARS)
        assert failed.value.kind == "timeout" and time.perf_counter() - t < 2
        assert (await runner.compute(*script(), {}, BARS))["out"].size == 10
    finally:
        await runner.stop()


@interactive_only
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
    runner = await job_worker(timeout_s=5)
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
        f"    r = IndicatorRunner(WindowsLauncher({JOB_IDENTITY!r}, Limits()), Limits())\n"
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
    launcher = WindowsLauncher(JOB_IDENTITY, limits, argv=argv, cwd=str(Path(argv[0]).parent))
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
            # A port that answers (as in CI-0 and in the server): "denied" then means the
            # container was stopped, not that nobody listened.
            listener = isolation._Listener()
            with socket.create_connection(("127.0.0.1", listener.port), timeout=2):
                pass  # reachable from outside the container
            targets = isolation.probe_targets(data, listener.port, "appcontainer")
            targets["read"]["read a .env in a user folder"] = str(tmp_path / ".env")
            targets["list"]["list the user profile"] = os.environ["USERPROFILE"]
            targets["credential"] = {"Credential Manager decoy": CREDENTIAL}
            targets["registry"] = {"registry decoy (HKCU)": REGISTRY}
            targets["write"] = {"write into the data folder": str(data)}
            # Its own folder: probed and reported (writable: docs/custom-indicators.md).
            own = launcher.profile_folder()
            assert own, "the test container has no folder of its own"
            targets["write"]["write into its own folder"] = own
            try:
                started = await fresh.start(targets)
            finally:
                listener.close()
        finally:
            del os.environ["OPENCHART_SECRET_KEY_FILE"]
        try:
            probes = started["probes"]
            report = os.environ.get("OPENCHART_TEST_REPORT_DIR")
            if report:  # CI prints it: every probe, as the worker saw it
                rows = [
                    f"{k}: {v.get('result')} ({v.get('detail', '')})" for k, v in probes.items()
                ]
                (Path(report) / "appcontainer-probes.txt").write_text(
                    "".join(r + "\n" for r in rows)
                )
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
            # Its own folder is writable (CI-1 investigation): the probe runs and is
            # reported (the table above); the server empties and caps it (B1,
            # tests/test_worker_runner.py).
            assert probes["write into its own folder"]["result"] in ("allowed", "denied", "error")
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


@interactive_only
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


@interactive_only
async def test_the_restricted_worker_on_a_desktop_of_its_own(tmp_path):
    """The restricted worker on a desktop of its own works where a desktop is usable.
    Without an interactive window station it fails even so (CI-1 on GitHub's runner)."""
    limits = Limits()
    launcher = WindowsLauncher("restricted", limits, private_desktop=True)
    runner, iso = await start_worker(tmp_path, limits, [launcher])
    assert runner is not None, iso.reason
    try:
        assert iso.level == "restricted" and not iso.minimum_met
        assert any("a private desktop of its own" in n for n in iso.notes)
        assert (await runner.compute(*script(), {}, BARS))["out"].size == 10
    finally:
        await runner.stop()


# D7 (a): a service install runs custom code in the AppContainer or not at all ------------


def service(monkeypatch, *, system=True, frozen_build=True, sid="real"):
    monkeypatch.setattr(windows, "running_as_system", lambda: system)
    monkeypatch.setattr(isolation, "frozen", lambda: frozen_build)
    if sid == "real":
        sid = windows.container_sid()
    if sid:
        monkeypatch.setenv(isolation.SANDBOX_SID_ENV, sid)
    else:
        monkeypatch.delenv(isolation.SANDBOX_SID_ENV, raising=False)


def test_a_service_install_gets_the_appcontainer_and_nothing_else(monkeypatch):
    service(monkeypatch)
    launchers = isolation.plan(Limits()).launchers
    assert [x.identity for x in launchers] == ["appcontainer"]
    assert launchers[0].private_desktop


@pytest.mark.parametrize(
    ("system", "frozen_build", "sid", "why"),
    [
        (True, False, "real", "need the release build"),  # a service on a source checkout
        (True, True, "", "not set up"),  # the installer's sandbox step did not run or failed
        (True, True, "S-1-15-2-1-2-3-4-5-6-7", "does not match"),
        (False, True, "real", None),  # installer-made but not LocalSystem: still a service
    ],
)
def test_a_service_install_without_its_sandbox_has_no_fallback(
    monkeypatch, system, frozen_build, sid, why
):
    service(monkeypatch, system=system, frozen_build=frozen_build, sid=sid)
    decided = isolation.plan(Limits())
    if why is None:
        assert [x.identity for x in decided.launchers] == ["appcontainer"]
    else:
        assert decided.launchers == [] and why in decided.unavailable


def failing_appcontainer(monkeypatch):
    """The AppContainer cannot be created: record every start, create nothing."""
    started = []

    def fake_start(argv, *, identity, **kwargs):
        started.append(identity)
        raise OSError(5, "CreateProcess: Access is denied")

    monkeypatch.setattr(windows, "start", fake_start)
    monkeypatch.setattr(WindowsLauncher, "container", lambda self: None)
    monkeypatch.setattr(WindowsLauncher, "profile_folder", lambda self: None)
    return started


async def test_when_the_appcontainer_cannot_start_a_service_runs_no_custom_code(
    monkeypatch, tmp_path
):
    from openchart.indicators._custom import CustomIndicators

    service(monkeypatch)
    started = failing_appcontainer(monkeypatch)
    runner, iso = await start_worker(tmp_path, Limits())
    assert runner is None and iso.level == "unavailable" and not iso.runs_custom_code
    assert set(started) == {"appcontainer"}  # never a restricted or plain worker
    assert "Access is denied" in iso.reason
    assert iso.notice.startswith("custom indicators do not run on this install")
    # The Custom section says why.
    folder = tmp_path / "indicators"
    folder.mkdir()
    (folder / "mine.py").write_text("NAME = 'Mine'\n")
    registry = CustomIndicators(folder)
    registry.worker_ready(None, iso)
    d = await registry.get("mine")
    assert d.status == "not_run" and d.reason == iso.notice


def test_the_catalog_shows_why_a_service_runs_no_custom_code(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from openchart.api.app import create_app
    from tests.conftest import Harness, run_setup

    service(monkeypatch)
    started = failing_appcontainer(monkeypatch)
    harness = Harness(tmp_path)
    harness.server._indicator_worker = True  # the real decision, through plan()
    (tmp_path / "indicators").mkdir()
    (tmp_path / "indicators" / "mine.py").write_text("NAME = 'Mine'\n")
    harness.seed_candles()
    with TestClient(create_app(harness.server)) as c:
        c.server = harness.server
        assert run_setup(c).status_code == 200
        end = time.monotonic() + 30
        while harness.server.isolation is None and time.monotonic() < end:
            time.sleep(0.1)
        body = c.get("/api/indicators").json()
        mine = {d["id"]: d for d in body["custom"]}["mine"]
        assert body["isolation"]["level"] == "unavailable"
        assert body["isolation"]["notice"].startswith("custom indicators do not run")
        assert mine["status"] == "not_run" and mine["reason"] == body["isolation"]["notice"]
        assert {"file": "mine.py", "error": mine["reason"], "kind": "custom"} in body["errors"]
        assert c.get("/api/indicators/mine", params={"instrument": "XAU_USD"}).status_code == 503
        assert body["editing"]["allowed"] is False
    assert set(started) == {"appcontainer"}


# A1: the plain worker follows only an observed STATUS_DLL_INIT_FAILED -------------------


def dying(code: int, limits: Limits) -> WindowsLauncher:
    """The restricted launcher, ending at start with this exit code. Started with the plain
    identity: where a restricted process cannot start at all (GitHub's runner), it would
    die with STATUS_DLL_INIT_FAILED before reaching the given code (found in CI-1). The
    decision under test only reads the launcher's level and the exit code."""
    exit_process = f"import ctypes; ctypes.windll.kernel32.ExitProcess(ctypes.c_uint({code}))"
    argv = [isolation.base_python(), "-I", "-c", exit_process]
    launcher = WindowsLauncher("plain", limits, argv=argv, cwd=str(Path(argv[0]).parent))
    launcher.level = "restricted"
    return launcher


class Counting(WindowsLauncher):
    started = 0

    def start(self):
        Counting.started += 1
        return super().start()


async def test_the_plain_worker_follows_exactly_the_desktop_failure(tmp_path):
    limits = Limits()
    Counting.started = 0
    runner, iso = await start_worker(
        tmp_path, limits, [dying(0xC0000142, limits), Counting("plain", limits)]
    )
    try:
        assert runner is not None and iso.level == "process-only" and not iso.minimum_met
        assert iso.notice == isolation.WEAKER_NOTICE
        assert isolation.RESTRICTED_FALLBACK in iso.notes[0] and Counting.started == 1
    finally:
        await runner.stop()


@pytest.mark.parametrize("code", [7, 0xC0000005, 0xC0000022])
async def test_any_other_restricted_failure_runs_no_custom_code(tmp_path, code):
    limits = Limits()
    Counting.started = 0
    runner, iso = await start_worker(
        tmp_path, limits, [dying(code, limits), Counting("plain", limits)]
    )
    assert runner is None and iso.level == "unavailable" and Counting.started == 0


async def test_a_service_never_takes_the_a1_fallback(monkeypatch, tmp_path):
    """D7 (a) holds even for the one failure A1 accepts elsewhere: the AppContainer of a
    service dying with STATUS_DLL_INIT_FAILED leaves no custom code running."""
    service(monkeypatch)
    started = []

    def fake_start(argv, *, identity, **kwargs):
        started.append(identity)
        raise OSError(0, f"the worker ended before it was ready: {isolation.RESTRICTED_FALLBACK}")

    monkeypatch.setattr(windows, "start", fake_start)
    monkeypatch.setattr(WindowsLauncher, "container", lambda self: None)
    monkeypatch.setattr(WindowsLauncher, "profile_folder", lambda self: None)
    runner, iso = await start_worker(tmp_path, Limits())
    assert runner is None and iso.level == "unavailable" and not iso.runs_custom_code
    assert set(started) == {"appcontainer"}
    assert iso.notice.startswith("custom indicators do not run on this install")


# Start priority: normal until the hello, below normal after it ------------------------------


def watch_jobs(runner: IndicatorRunner) -> list[str | None]:
    """The worker's priority class as each compute request goes out and again when its
    answer is back: sampled while the request is outstanding, without a job that has to wait
    for the clock (a worker below normal priority wakes late from a sleep on a busy machine,
    but answers a request at once)."""
    seen: list[str | None] = []
    ask = runner._ask

    async def spy(live, header, arrays, wait):
        if header.get("cmd") != "compute":
            return await ask(live, header, arrays, wait)
        seen.append(live.handle.priority())
        got = await ask(live, header, arrays, wait)
        seen.append(live.handle.priority())
        return got

    runner._ask = spy
    return seen


async def test_a_real_worker_starts_at_normal_priority_and_runs_below_normal():
    limits = Limits(timeout_s=10)
    launcher = WindowsLauncher(JOB_IDENTITY, limits)
    assert launcher.lowers_priority_after_hello
    bare = launcher.start()
    try:
        assert bare.priority() == "normal"  # what the server lowers after the hello
        assert bare.cpu_seconds() is not None
    finally:
        bare.kill()
        bare.close()
    runner = IndicatorRunner(launcher, limits)
    await runner.start()
    seen = watch_jobs(runner)
    try:
        assert runner._live.handle.priority() == "below_normal"
        await runner.compute(*script(), {}, BARS)
        runner.kill_for_tests()  # a respawn follows the same path
        runner._live.handle.wait(5000)
        await runner.compute(*script(), {}, BARS)
        assert runner.restarts == 1
        assert seen == ["below_normal"] * 4  # before and after each job, on both workers
    finally:
        await runner.stop()


def busy_loops(count: int) -> list[subprocess.Popen]:
    """One normal-priority busy loop per core, each ending by itself after ten minutes."""
    code = "import time\nt = time.time()\nwhile time.time() - t < 600: pass"
    return [
        subprocess.Popen(
            [isolation.base_python(), "-I", "-c", code],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(count)
    ]


async def test_a_worker_becomes_ready_when_every_core_is_busy():
    """The case that failed (docs/investigations/worker-start-under-load.md): a worker
    created below normal priority got 0.4 s of CPU in 150 s next to as many normal busy
    loops as cores. It must now say hello within the start deadline, then run below normal.
    The deadline is generous (90 s; 1 to 7 s measured), so a slow runner does not flake."""
    from openchart.indicators.worker import runner as runner_module

    loops = busy_loops(os.cpu_count() or 1)
    try:
        await asyncio.sleep(1.0)
        assert all(p.poll() is None for p in loops)
        limits = Limits(timeout_s=30)
        runner = IndicatorRunner(WindowsLauncher(JOB_IDENTITY, limits), limits)
        t = time.perf_counter()
        await runner.start()  # raises when no hello comes within START_TIMEOUT
        try:
            assert runner.state == "ready"
            assert time.perf_counter() - t < runner_module.START_TIMEOUT
            seen = watch_jobs(runner)
            await runner.compute(*script(), {}, BARS)
            assert seen == ["below_normal"] * 2  # during a job
        finally:
            # A graceful exit needs CPU the below-normal worker does not get on a busy
            # machine (about 40 s measured); the job object ends it at once.
            runner.kill_for_tests()
            await runner.stop()
    finally:
        for p in loops:
            p.kill()
        for p in loops:
            p.wait(10)
