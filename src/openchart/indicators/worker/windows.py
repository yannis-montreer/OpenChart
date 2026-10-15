"""Starting the indicator worker on Windows with less than the server can do (ctypes).

From the CI-0 spike (`docs/ci0-spike-report.md`), findings 1 to 10:

- **A job object** around every worker: a memory limit, one process only (no child
  programs), a hard CPU cap, killed when the job handle closes (so when the server dies),
  no error dialogs, UI restrictions. Wall-clock deadlines are the runner's.
- **An identity:**
  - `appcontainer`: no capabilities, so no network (loopback included) and files only
    where its SID is granted (the installer grants the release folder). It needs
    `LOCALAPPDATA` in its environment (finding 1) and, when the server is a service, a
    window station and desktop of its own (finding 7).
  - `restricted`: the server's token without privileges, Administrators and SYSTEM
    deny-only, Low integrity. For source installs, below the minimum (finding 5); a
    frozen build cannot start at Low, so it keeps the server's level (finding 9).
  - `plain`: the server's own token, still inside the job (process only).
- Pipes for stdin, stdout and stderr; an environment block built from nothing.
"""

from __future__ import annotations

import contextlib
import ctypes as C
import itertools
import msvcrt
import os
import subprocess
from ctypes import wintypes as W
from dataclasses import dataclass, field

k32 = C.WinDLL("kernel32", use_last_error=True)
adv = C.WinDLL("advapi32", use_last_error=True)
uenv = C.WinDLL("userenv", use_last_error=True)
u32 = C.WinDLL("user32", use_last_error=True)
ole32 = C.WinDLL("ole32")

HANDLE, DWORD, BOOL, PVOID = W.HANDLE, W.DWORD, W.BOOL, C.c_void_p

CONTAINER_NAME = "OpenChart.IndicatorWorker"
ADMINISTRATORS, LOCAL_SYSTEM = "S-1-5-32-544", "S-1-5-18"
KNOWN_EXIT_CODES = {
    0xC0000142: "STATUS_DLL_INIT_FAILED (no usable desktop)",
    0xC0000022: "STATUS_ACCESS_DENIED (the runtime folder is not readable for it)",
    0xC0000005: "STATUS_ACCESS_VIOLATION",
    0xC0000409: "STATUS_STACK_BUFFER_OVERRUN",
    0xC00000FD: "STATUS_STACK_OVERFLOW",
    0xC0000017: "STATUS_NO_MEMORY",
}


class SID_AND_ATTRIBUTES(C.Structure):
    _fields_ = [("Sid", PVOID), ("Attributes", DWORD)]


class SECURITY_CAPABILITIES(C.Structure):
    _fields_ = [
        ("AppContainerSid", PVOID),
        ("Capabilities", C.POINTER(SID_AND_ATTRIBUTES)),
        ("CapabilityCount", DWORD),
        ("Reserved", DWORD),
    ]


class STARTUPINFOW(C.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", W.LPWSTR),
        ("lpDesktop", W.LPWSTR),
        ("lpTitle", W.LPWSTR),
        ("dwX", DWORD),
        ("dwY", DWORD),
        ("dwXSize", DWORD),
        ("dwYSize", DWORD),
        ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", W.WORD),
        ("cbReserved2", W.WORD),
        ("lpReserved2", PVOID),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class STARTUPINFOEXW(C.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", PVOID)]


class PROCESS_INFORMATION(C.Structure):
    _fields_ = [
        ("hProcess", HANDLE),
        ("hThread", HANDLE),
        ("dwProcessId", DWORD),
        ("dwThreadId", DWORD),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(C.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", C.c_int64),
        ("PerJobUserTimeLimit", C.c_int64),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", C.c_size_t),
        ("MaximumWorkingSetSize", C.c_size_t),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", C.c_size_t),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class IO_COUNTERS(C.Structure):
    _fields_ = [(n, C.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(C.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", C.c_size_t),
        ("JobMemoryLimit", C.c_size_t),
        ("PeakProcessMemoryUsed", C.c_size_t),
        ("PeakJobMemoryUsed", C.c_size_t),
    ]


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(C.Structure):
    _fields_ = [("ControlFlags", DWORD), ("CpuRate", DWORD)]


class SECURITY_ATTRIBUTES(C.Structure):
    _fields_ = [("nLength", DWORD), ("lpSecurityDescriptor", PVOID), ("bInheritHandle", BOOL)]


def _sig(dll, name, res, *args):
    f = getattr(dll, name)
    f.restype, f.argtypes = res, list(args)
    return f


CreateJobObjectW = _sig(k32, "CreateJobObjectW", HANDLE, PVOID, W.LPCWSTR)
SetInformationJobObject = _sig(k32, "SetInformationJobObject", BOOL, HANDLE, C.c_int, PVOID, DWORD)
QueryInformationJobObject = _sig(
    k32, "QueryInformationJobObject", BOOL, HANDLE, C.c_int, PVOID, DWORD, PVOID
)
AssignProcessToJobObject = _sig(k32, "AssignProcessToJobObject", BOOL, HANDLE, HANDLE)
TerminateJobObject = _sig(k32, "TerminateJobObject", BOOL, HANDLE, W.UINT)
InitializeProcThreadAttributeList = _sig(
    k32, "InitializeProcThreadAttributeList", BOOL, PVOID, DWORD, DWORD, C.POINTER(C.c_size_t)
)
UpdateProcThreadAttribute = _sig(
    k32,
    "UpdateProcThreadAttribute",
    BOOL,
    PVOID,
    DWORD,
    C.c_size_t,
    PVOID,
    C.c_size_t,
    PVOID,
    PVOID,
)
DeleteProcThreadAttributeList = _sig(k32, "DeleteProcThreadAttributeList", None, PVOID)
_PROC_ARGS = (W.LPCWSTR, W.LPWSTR, PVOID, PVOID, BOOL, DWORD, PVOID, W.LPCWSTR, PVOID)
CreateProcessW = _sig(k32, "CreateProcessW", BOOL, *_PROC_ARGS, C.POINTER(PROCESS_INFORMATION))
CreateProcessAsUserW = _sig(
    adv, "CreateProcessAsUserW", BOOL, HANDLE, *_PROC_ARGS, C.POINTER(PROCESS_INFORMATION)
)
ResumeThread = _sig(k32, "ResumeThread", DWORD, HANDLE)
WaitForSingleObject = _sig(k32, "WaitForSingleObject", DWORD, HANDLE, DWORD)
GetExitCodeProcess = _sig(k32, "GetExitCodeProcess", BOOL, HANDLE, C.POINTER(DWORD))
CloseHandle = _sig(k32, "CloseHandle", BOOL, HANDLE)
GetCurrentProcess = _sig(k32, "GetCurrentProcess", HANDLE)
TerminateProcess = _sig(k32, "TerminateProcess", BOOL, HANDLE, W.UINT)
LocalFree = _sig(k32, "LocalFree", PVOID, PVOID)
OpenProcessToken = _sig(adv, "OpenProcessToken", BOOL, HANDLE, DWORD, C.POINTER(HANDLE))
GetTokenInformation = _sig(
    adv, "GetTokenInformation", BOOL, HANDLE, C.c_int, PVOID, DWORD, C.POINTER(DWORD)
)
CreateRestrictedToken = _sig(
    adv,
    "CreateRestrictedToken",
    BOOL,
    HANDLE,
    DWORD,
    DWORD,
    PVOID,
    DWORD,
    PVOID,
    DWORD,
    PVOID,
    C.POINTER(HANDLE),
)
SetTokenInformation = _sig(adv, "SetTokenInformation", BOOL, HANDLE, C.c_int, PVOID, DWORD)
ConvertStringSidToSidW = _sig(adv, "ConvertStringSidToSidW", BOOL, W.LPCWSTR, C.POINTER(PVOID))
ConvertSidToStringSidW = _sig(adv, "ConvertSidToStringSidW", BOOL, PVOID, C.POINTER(W.LPWSTR))
GetLengthSid = _sig(adv, "GetLengthSid", DWORD, PVOID)
FreeSid = _sig(adv, "FreeSid", PVOID, PVOID)
ConvertSddl = _sig(
    adv,
    "ConvertStringSecurityDescriptorToSecurityDescriptorW",
    BOOL,
    W.LPCWSTR,
    DWORD,
    C.POINTER(PVOID),
    PVOID,
)
CreateAppContainerProfile = _sig(
    uenv,
    "CreateAppContainerProfile",
    C.c_long,
    W.LPCWSTR,
    W.LPCWSTR,
    W.LPCWSTR,
    PVOID,
    DWORD,
    C.POINTER(PVOID),
)
DeriveAppContainerSidFromAppContainerName = _sig(
    uenv, "DeriveAppContainerSidFromAppContainerName", C.c_long, W.LPCWSTR, C.POINTER(PVOID)
)
DeleteAppContainerProfile = _sig(uenv, "DeleteAppContainerProfile", C.c_long, W.LPCWSTR)
GetAppContainerFolderPath = _sig(
    uenv, "GetAppContainerFolderPath", C.c_long, W.LPCWSTR, C.POINTER(W.LPWSTR)
)
CoTaskMemFree = _sig(ole32, "CoTaskMemFree", None, PVOID)
GetProcessWindowStation = _sig(u32, "GetProcessWindowStation", HANDLE)
SetProcessWindowStation = _sig(u32, "SetProcessWindowStation", BOOL, HANDLE)
CreateWindowStationW = _sig(u32, "CreateWindowStationW", HANDLE, W.LPCWSTR, DWORD, DWORD, PVOID)
CreateDesktopW = _sig(u32, "CreateDesktopW", HANDLE, W.LPCWSTR, PVOID, PVOID, DWORD, DWORD, PVOID)
CloseWindowStation = _sig(u32, "CloseWindowStation", BOOL, HANDLE)
CloseDesktop = _sig(u32, "CloseDesktop", BOOL, HANDLE)
GetUserObjectInformationW = _sig(
    u32, "GetUserObjectInformationW", BOOL, HANDLE, C.c_int, PVOID, DWORD, PVOID
)

JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x8
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
EXTENDED_STARTUPINFO_PRESENT = 0x80000
CREATE_SUSPENDED = 0x4
CREATE_UNICODE_ENVIRONMENT = 0x400
CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x4000
STARTF_USESTDHANDLES = 0x100
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x20002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x20009
STILL_ACTIVE = 259
ALREADY_EXISTS = C.c_long(0x800700B7).value
_desktops = itertools.count(1)


def _check(ok, what: str):
    if not ok:
        err = C.get_last_error()
        raise OSError(err, f"{what}: {C.FormatError(err).strip()}")
    return ok


def _hr(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(hr & 0xFFFF, f"{what}: HRESULT {hr & 0xFFFFFFFF:#010x}")


def describe_exit_code(code: int) -> str:
    known = KNOWN_EXIT_CODES.get(code & 0xFFFFFFFF)
    return f"exit code {code & 0xFFFFFFFF:#010x}" + (f" {known}" if known else "")


# Identities -----------------------------------------------------------------------------


def _sid(text: str) -> PVOID:
    sid = PVOID()
    _check(ConvertStringSidToSidW(text, C.byref(sid)), f"ConvertStringSidToSid {text}")
    return sid  # freed with LocalFree by the caller, or kept for the process lifetime


def sid_to_string(sid: PVOID) -> str:
    out = W.LPWSTR()
    _check(ConvertSidToStringSidW(sid, C.byref(out)), "ConvertSidToStringSid")
    text = out.value
    LocalFree(C.cast(out, PVOID))
    return text


def running_as_system() -> bool:
    """True for a Windows service running as LocalSystem."""
    token = HANDLE()
    _check(OpenProcessToken(GetCurrentProcess(), 0x0008, C.byref(token)), "OpenProcessToken")
    try:
        buf = C.create_string_buffer(256)
        n = DWORD()
        _check(GetTokenInformation(token, 1, buf, 256, C.byref(n)), "TokenUser")
        sid = C.cast(buf, C.POINTER(PVOID))[0]
        return sid_to_string(sid) == LOCAL_SYSTEM
    finally:
        CloseHandle(token)


def container_sid(name: str = CONTAINER_NAME) -> str:
    """The container's SID: it depends on the name only, so it is the same for every
    account (the installer grants it the release folder before the service exists)."""
    sid = PVOID()
    _hr(DeriveAppContainerSidFromAppContainerName(name, C.byref(sid)), "DeriveAppContainerSid")
    try:
        return sid_to_string(sid)
    finally:
        FreeSid(sid)


def ensure_profile(name: str = CONTAINER_NAME) -> PVOID:
    """The container's SID, creating its profile for this account on first use
    (idempotent; as SYSTEM for the service, CI-0 run 3)."""
    sid = PVOID()
    hr = CreateAppContainerProfile(
        name, name, "OpenChart indicator worker: runs custom indicators", None, 0, C.byref(sid)
    )
    if hr == ALREADY_EXISTS:
        hr = DeriveAppContainerSidFromAppContainerName(name, C.byref(sid))
    _hr(hr, "CreateAppContainerProfile")
    return sid


def delete_profile(name: str = CONTAINER_NAME) -> int:
    """HRESULT: 0, or an error when there was nothing to delete."""
    return DeleteAppContainerProfile(name)


def profile_folder(sid_text: str) -> str | None:
    """The container's own folder (its TEMP), for this account; None if it has none."""
    out = W.LPWSTR()
    if GetAppContainerFolderPath(sid_text, C.byref(out)) < 0:
        return None
    try:
        return out.value
    finally:
        CoTaskMemFree(C.cast(out, PVOID))


def _icacls(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["icacls", *args], capture_output=True, text=True, errors="replace", check=False
    )


def deny_profile_writes(folder: str, sid_text: str) -> None:
    """Finding 2: the one write an AppContainer has is its own folder; a script could fill
    the disk there. The worker does not need it."""
    r = _icacls(folder, "/deny", f"*{sid_text}:(OI)(CI)(W,D,DC)", "/C", "/Q")
    if r.returncode:
        raise OSError(r.returncode, f"icacls {folder}: {(r.stdout + r.stderr).strip()}")


def allow_profile_writes(folder: str, sid_text: str) -> None:
    _icacls(folder, "/remove:d", f"*{sid_text}", "/C", "/Q")


def restricted_token(*, low: bool) -> HANDLE:
    """This process's token without privileges, Administrators and SYSTEM deny-only, and
    with `low`, at Low integrity."""
    mine = HANDLE()
    _check(OpenProcessToken(GetCurrentProcess(), 0x000F01FF, C.byref(mine)), "OpenProcessToken")
    sids = [_sid(ADMINISTRATORS), _sid(LOCAL_SYSTEM)]
    table = (SID_AND_ATTRIBUTES * len(sids))(*[SID_AND_ATTRIBUTES(s, 0) for s in sids])
    new = HANDLE()
    try:
        _check(
            CreateRestrictedToken(
                mine, 1, len(sids), C.byref(table), 0, None, 0, None, C.byref(new)
            ),
            "CreateRestrictedToken",
        )
    finally:
        CloseHandle(mine)
        for s in sids:
            LocalFree(s)
    if low:
        label_sid = _sid("S-1-16-4096")
        label = SID_AND_ATTRIBUTES(label_sid, 0x20)  # SE_GROUP_INTEGRITY
        try:
            _check(
                SetTokenInformation(
                    new, 25, C.byref(label), C.sizeof(label) + GetLengthSid(label_sid)
                ),
                "set Low integrity",
            )
        finally:
            LocalFree(label_sid)
    return new


# A private window station and desktop (finding 7) ----------------------------------------


def make_private_desktop(name: str, container: str | None) -> tuple[HANDLE, HANDLE, str]:
    """A service's own desktop admits only SYSTEM and Administrators, so an AppContainer
    dies loading user32 there (STATUS_DLL_INIT_FAILED). The worker gets an empty one, open
    to everyone, all app packages and the container, with a Low label. Returns the
    handles (kept open while the worker runs) and "winstation\\desktop"."""
    aces = "(A;;GA;;;WD)(A;;GA;;;AC)(A;;GA;;;SY)" + (f"(A;;GA;;;{container})" if container else "")
    sd = PVOID()
    _check(ConvertSddl(f"D:{aces}S:(ML;;NW;;;LW)", 1, C.byref(sd), None), "security descriptor")
    sa = SECURITY_ATTRIBUTES(C.sizeof(SECURITY_ATTRIBUTES), sd, False)
    old = GetProcessWindowStation()
    try:
        # A named one (SYSTEM may create it); a non-elevated user may not, and gets the
        # unnamed one of its logon session, with a private desktop inside.
        winsta = CreateWindowStationW(name, 0, 0x37F, C.byref(sa))  # WINSTA_ALL_ACCESS
        if not winsta and C.get_last_error() == 5:
            winsta = CreateWindowStationW(None, 0, 0x37F, C.byref(sa))
        _check(winsta, "CreateWindowStation")
        buf = C.create_unicode_buffer(256)
        _check(
            GetUserObjectInformationW(winsta, 2, buf, C.sizeof(buf), None), "window station name"
        )
        _check(SetProcessWindowStation(winsta), "SetProcessWindowStation")
        try:
            desk = CreateDesktopW(name, None, None, 0, 0x10000000, C.byref(sa))  # GENERIC_ALL
            _check(desk, "CreateDesktop")
        finally:
            SetProcessWindowStation(old)
    finally:
        LocalFree(sd)
    return winsta, desk, buf.value + "\\" + name


# The job object -------------------------------------------------------------------------


def make_job(memory_mb: int, cpu_percent_of_one_core: float) -> HANDLE:
    job = CreateJobObjectW(None, None)
    _check(job, "CreateJobObject")
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | JOB_OBJECT_LIMIT_JOB_MEMORY
        | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    info.BasicLimitInformation.ActiveProcessLimit = 1
    info.ProcessMemoryLimit = info.JobMemoryLimit = memory_mb * 2**20
    try:
        _check(SetInformationJobObject(job, 9, C.byref(info), C.sizeof(info)), "job limits")
        cores = os.cpu_count() or 1
        rate = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(
            0x1 | 0x4,  # ENABLE | HARD_CAP; CpuRate is in 1/100 % of the whole machine
            max(1, min(10_000, int(cpu_percent_of_one_core * 100 / cores))),
        )
        _check(SetInformationJobObject(job, 15, C.byref(rate), C.sizeof(rate)), "job CPU cap")
        ui = DWORD(0xFF)  # every JOB_OBJECT_UILIMIT_* flag
        _check(SetInformationJobObject(job, 4, C.byref(ui), C.sizeof(ui)), "job UI limits")
    except OSError:
        CloseHandle(job)
        raise
    return job


def job_peak_mb(job: HANDLE) -> float:
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    _check(QueryInformationJobObject(job, 9, C.byref(info), C.sizeof(info), None), "job info")
    return info.PeakProcessMemoryUsed / 2**20


# Starting a worker ----------------------------------------------------------------------


def env_block(env: dict[str, str]) -> C.Array:
    text = "".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda kv: kv[0].upper()))
    text += "\0"
    return C.create_unicode_buffer(text, len(text))


@dataclass
class WinProcess:
    pid: int
    process: HANDLE
    job: HANDLE
    stdin: object  # binary file, server -> worker
    stdout: object  # binary file, worker -> server
    stderr: object  # binary file, the worker's own messages
    desktop: tuple = field(default=())
    closed: bool = False

    def alive(self) -> bool:
        code = DWORD()
        GetExitCodeProcess(self.process, C.byref(code))
        return code.value == STILL_ACTIVE

    def wait(self, ms: int) -> bool:
        return WaitForSingleObject(self.process, ms) == 0

    def exit_code(self) -> int | None:
        code = DWORD()
        GetExitCodeProcess(self.process, C.byref(code))
        return None if code.value == STILL_ACTIVE else code.value

    def kill(self) -> None:
        if not self.closed:
            TerminateJobObject(self.job, 1)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for f in (self.stdin, self.stdout, self.stderr):
            with contextlib.suppress(OSError):
                f.close()
        CloseHandle(self.process)
        CloseHandle(self.job)  # KILL_ON_JOB_CLOSE ends anything left
        if self.desktop:
            CloseDesktop(self.desktop[1])
            CloseWindowStation(self.desktop[0])


def start(
    argv: list[str],
    *,
    identity: str,
    env: dict[str, str],
    cwd: str,
    container: PVOID | None = None,
    memory_mb: int = 512,
    cpu_percent_of_one_core: float = 50,
    private_desktop: bool = False,
    low_integrity: bool = True,
) -> WinProcess:
    """Start `argv` suspended, put it in its job, then let it run."""
    if identity not in ("appcontainer", "restricted", "plain"):
        raise ValueError(identity)
    if identity == "appcontainer" and container is None:
        raise ValueError("an AppContainer worker needs its container SID")
    job = make_job(memory_mb, cpu_percent_of_one_core)
    fds = [os.pipe() for _ in range(3)]  # (read, write) each: stdin, stdout, stderr
    child_ends = [fds[0][0], fds[1][1], fds[2][1]]
    parent_ends = [fds[0][1], fds[1][0], fds[2][0]]
    child = [msvcrt.get_osfhandle(fd) for fd in child_ends]
    for h in child:
        os.set_handle_inheritable(h, True)

    si = STARTUPINFOEXW()
    si.StartupInfo.cb = C.sizeof(si)
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
    si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = child
    desktop: tuple = ()
    keep: list = []  # buffers that must outlive CreateProcess
    try:
        if private_desktop:
            sid_text = sid_to_string(container) if identity == "appcontainer" else None
            winsta, desk, desk_name = make_private_desktop(
                f"OpenChartWorker{next(_desktops)}", sid_text
            )
            desktop = (winsta, desk, desk_name)
            keep.append(desktop_buf := C.create_unicode_buffer(desk_name))
            si.StartupInfo.lpDesktop = C.cast(desktop_buf, W.LPWSTR)
        count = 2 if identity == "appcontainer" else 1
        size = C.c_size_t()
        InitializeProcThreadAttributeList(None, count, 0, C.byref(size))
        attrs = C.create_string_buffer(size.value)
        _check(
            InitializeProcThreadAttributeList(C.byref(attrs), count, 0, C.byref(size)), "attributes"
        )
        try:
            handles = (HANDLE * 3)(*child)
            _check(
                UpdateProcThreadAttribute(
                    C.byref(attrs),
                    0,
                    PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                    C.byref(handles),
                    C.sizeof(handles),
                    None,
                    None,
                ),
                "handle list",
            )
            if identity == "appcontainer":
                caps = SECURITY_CAPABILITIES(container, None, 0, 0)
                keep.append(caps)
                _check(
                    UpdateProcThreadAttribute(
                        C.byref(attrs),
                        0,
                        PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                        C.byref(caps),
                        C.sizeof(caps),
                        None,
                        None,
                    ),
                    "security capabilities",
                )
            si.lpAttributeList = C.addressof(attrs)
            flags = (
                EXTENDED_STARTUPINFO_PRESENT
                | CREATE_SUSPENDED
                | CREATE_NO_WINDOW
                | BELOW_NORMAL_PRIORITY_CLASS
                | CREATE_UNICODE_ENVIRONMENT
            )
            block = env_block(env)
            cmd = C.create_unicode_buffer(subprocess.list2cmdline(argv))
            pi = PROCESS_INFORMATION()
            if identity == "restricted":
                token = restricted_token(low=low_integrity)
                try:
                    _check(
                        CreateProcessAsUserW(
                            token,
                            None,
                            cmd,
                            None,
                            None,
                            True,
                            flags,
                            C.byref(block),
                            cwd,
                            C.byref(si),
                            C.byref(pi),
                        ),
                        "CreateProcessAsUser",
                    )
                finally:
                    CloseHandle(token)
            else:
                _check(
                    CreateProcessW(
                        None,
                        cmd,
                        None,
                        None,
                        True,
                        flags,
                        C.byref(block),
                        cwd,
                        C.byref(si),
                        C.byref(pi),
                    ),
                    "CreateProcess",
                )
        finally:
            DeleteProcThreadAttributeList(C.byref(attrs))
        try:
            _check(AssignProcessToJobObject(job, pi.hProcess), "AssignProcessToJobObject")
        except OSError:
            TerminateProcess(pi.hProcess, 1)
            CloseHandle(pi.hThread)
            CloseHandle(pi.hProcess)
            raise
        ResumeThread(pi.hThread)
        CloseHandle(pi.hThread)
    except BaseException:
        for fd in parent_ends:
            os.close(fd)
        CloseHandle(job)
        if desktop:
            CloseDesktop(desktop[1])
            CloseWindowStation(desktop[0])
        raise
    finally:
        for fd in child_ends:
            os.close(fd)
    return WinProcess(
        pi.dwProcessId,
        pi.hProcess,
        job,
        os.fdopen(parent_ends[0], "wb", buffering=0),
        os.fdopen(parent_ends[1], "rb", buffering=0),
        os.fdopen(parent_ends[2], "rb", buffering=0),
        desktop,
    )
