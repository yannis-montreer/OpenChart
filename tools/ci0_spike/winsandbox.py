"""CI-0 spike: start a worker process with less than its parent can do (Windows, ctypes).

- a job object: memory limit, one process only (no child programs), a hard CPU cap, killed
  when the job handle closes (the parent dies), no error dialogs, UI restrictions;
- an identity: "appcontainer" (no capabilities: no network, files only where its SID is
  granted), "restricted" (privileges removed, Administrators and SYSTEM deny-only, Low
  integrity) or "plain" (the parent's own token, for comparison);
- a clean environment block and pipes for stdin/stdout; stderr goes to a log file.
"""

from __future__ import annotations

import contextlib
import ctypes as C
import itertools
import msvcrt
import os
import subprocess
from ctypes import wintypes as W
from dataclasses import dataclass

k32 = C.WinDLL("kernel32", use_last_error=True)
adv = C.WinDLL("advapi32", use_last_error=True)
uenv = C.WinDLL("userenv", use_last_error=True)

HANDLE, DWORD, BOOL, PVOID = W.HANDLE, W.DWORD, W.BOOL, C.c_void_p


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


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(C.Structure):
    _fields_ = [
        ("TotalUserTime", C.c_int64),
        ("TotalKernelTime", C.c_int64),
        ("ThisPeriodTotalUserTime", C.c_int64),
        ("ThisPeriodTotalKernelTime", C.c_int64),
        ("TotalPageFaultCount", DWORD),
        ("TotalProcesses", DWORD),
        ("ActiveProcesses", DWORD),
        ("TotalTerminatedProcesses", DWORD),
    ]


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
CreateProcessW = _sig(
    k32,
    "CreateProcessW",
    BOOL,
    W.LPCWSTR,
    W.LPWSTR,
    PVOID,
    PVOID,
    BOOL,
    DWORD,
    PVOID,
    W.LPCWSTR,
    PVOID,
    C.POINTER(PROCESS_INFORMATION),
)
CreateProcessAsUserW = _sig(
    adv,
    "CreateProcessAsUserW",
    BOOL,
    HANDLE,
    W.LPCWSTR,
    W.LPWSTR,
    PVOID,
    PVOID,
    BOOL,
    DWORD,
    PVOID,
    W.LPCWSTR,
    PVOID,
    C.POINTER(PROCESS_INFORMATION),
)
ResumeThread = _sig(k32, "ResumeThread", DWORD, HANDLE)
WaitForSingleObject = _sig(k32, "WaitForSingleObject", DWORD, HANDLE, DWORD)
GetExitCodeProcess = _sig(k32, "GetExitCodeProcess", BOOL, HANDLE, C.POINTER(DWORD))
CloseHandle = _sig(k32, "CloseHandle", BOOL, HANDLE)
GetCurrentProcess = _sig(k32, "GetCurrentProcess", HANDLE)
OpenProcess = _sig(k32, "OpenProcess", HANDLE, DWORD, BOOL, DWORD)
TerminateProcess = _sig(k32, "TerminateProcess", BOOL, HANDLE, W.UINT)
LocalFree = _sig(k32, "LocalFree", PVOID, PVOID)
OpenProcessToken = _sig(adv, "OpenProcessToken", BOOL, HANDLE, DWORD, C.POINTER(HANDLE))
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


def _check(ok, what: str):
    if not ok:
        err = C.get_last_error()
        raise OSError(err, f"{what}: {C.FormatError(err)}")
    return ok


def _hr(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(hr & 0xFFFF, f"{what}: HRESULT {hr & 0xFFFFFFFF:#010x}")


def sid_from_string(text: str) -> PVOID:
    sid = PVOID()
    _check(ConvertStringSidToSidW(text, C.byref(sid)), f"ConvertStringSidToSid {text}")
    return sid  # LocalFree when done; kept for the process lifetime here


def sid_to_string(sid: PVOID) -> str:
    out = W.LPWSTR()
    _check(ConvertSidToStringSidW(sid, C.byref(out)), "ConvertSidToStringSid")
    text = out.value
    LocalFree(C.cast(out, PVOID))
    return text


# AppContainer profile -----------------------------------------------------------------


def appcontainer_sid(name: str) -> PVOID:
    """The container's SID, creating its profile on first use."""
    sid = PVOID()
    hr = CreateAppContainerProfile(
        name, name, "OpenChart indicator worker (spike)", None, 0, C.byref(sid)
    )
    if hr == ALREADY_EXISTS:
        hr = DeriveAppContainerSidFromAppContainerName(name, C.byref(sid))
    _hr(hr, "CreateAppContainerProfile")
    return sid


def delete_appcontainer(name: str) -> int:
    return DeleteAppContainerProfile(name)


def grant_folder(folder: str, sid_text: str) -> None:
    """Read and execute for the container on one folder tree (copies made for the test)."""
    r = subprocess.run(
        ["icacls", folder, "/grant", f"*{sid_text}:(OI)(CI)RX", "/T", "/C", "/Q"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if r.returncode:
        raise OSError(r.returncode, f"icacls {folder}: {r.stdout.strip()} {r.stderr.strip()}")


# Restricted token ---------------------------------------------------------------------


def restricted_token(disable: list[str], *, low: bool = True, max_privilege: bool = True) -> HANDLE:
    """The parent's token minus its privileges, with `disable` SIDs deny-only, at Low
    integrity."""
    mine = HANDLE()
    _check(OpenProcessToken(GetCurrentProcess(), 0x000F01FF, C.byref(mine)), "OpenProcessToken")
    sids = (SID_AND_ATTRIBUTES * len(disable))(
        *[SID_AND_ATTRIBUTES(sid_from_string(s), 0) for s in disable]
    )
    new = HANDLE()
    _check(
        CreateRestrictedToken(
            mine,
            1 if max_privilege else 0,
            len(disable),
            C.byref(sids),
            0,
            None,
            0,
            None,
            C.byref(new),
        ),
        "CreateRestrictedToken",
    )
    CloseHandle(mine)
    if not low:
        return new
    low = sid_from_string("S-1-16-4096")
    label = SID_AND_ATTRIBUTES(low, 0x20)  # SE_GROUP_INTEGRITY
    _check(
        SetTokenInformation(new, 25, C.byref(label), C.sizeof(label) + GetLengthSid(low)),
        "set Low integrity",
    )
    return new


# A private window station and desktop ----------------------------------------------------
# A service's own desktop (session 0) only admits SYSTEM and Administrators, so a worker
# whose token has those deny-only, or an AppContainer, cannot attach to it and dies while
# loading user32 (STATUS_DLL_INIT_FAILED). The worker gets a desktop of its own instead:
# empty, and open to the identities it runs as.

u32 = C.WinDLL("user32", use_last_error=True)
_desktops = itertools.count(1)
GetProcessWindowStation = _sig(u32, "GetProcessWindowStation", HANDLE)
SetProcessWindowStation = _sig(u32, "SetProcessWindowStation", BOOL, HANDLE)
CreateWindowStationW = _sig(u32, "CreateWindowStationW", HANDLE, W.LPCWSTR, DWORD, DWORD, PVOID)
CreateDesktopW = _sig(u32, "CreateDesktopW", HANDLE, W.LPCWSTR, PVOID, PVOID, DWORD, DWORD, PVOID)
CloseWindowStation = _sig(u32, "CloseWindowStation", BOOL, HANDLE)
CloseDesktop = _sig(u32, "CloseDesktop", BOOL, HANDLE)
GetUserObjectInformationW = _sig(
    u32, "GetUserObjectInformationW", BOOL, HANDLE, C.c_int, PVOID, DWORD, PVOID
)
ConvertSddl = _sig(
    adv,
    "ConvertStringSecurityDescriptorToSecurityDescriptorW",
    BOOL,
    W.LPCWSTR,
    DWORD,
    C.POINTER(PVOID),
    PVOID,
)


class SECURITY_ATTRIBUTES(C.Structure):
    _fields_ = [("nLength", DWORD), ("lpSecurityDescriptor", PVOID), ("bInheritHandle", BOOL)]


def make_private_desktop(name: str, container_sid: str | None) -> tuple[HANDLE, HANDLE, str]:
    """Everyone, all app packages and the container: full access; a Low label so a Low
    integrity worker may use it too. Returns the handles (keep them open) and the
    "winstation/desktop" name (with a backslash) for STARTUPINFO.lpDesktop."""
    aces = "(A;;GA;;;WD)(A;;GA;;;AC)(A;;GA;;;SY)" + (
        f"(A;;GA;;;{container_sid})" if container_sid else ""
    )
    sd = PVOID()
    _check(ConvertSddl(f"D:{aces}S:(ML;;NW;;;LW)", 1, C.byref(sd), None), "security descriptor")
    sa = SECURITY_ATTRIBUTES(C.sizeof(SECURITY_ATTRIBUTES), sd, False)
    old = GetProcessWindowStation()
    # A named one of our own (SYSTEM may create it). A non-elevated user may not (access
    # denied, found in CI-0); it then gets the unnamed one of its logon session, with a
    # private desktop inside. As SYSTEM the unnamed one would be the services' shared
    # window station, the one the worker cannot use, so the named one is what matters there.
    winsta = CreateWindowStationW(name, 0, 0x37F, C.byref(sa))  # WINSTA_ALL_ACCESS
    if not winsta and C.get_last_error() == 5:
        winsta = CreateWindowStationW(None, 0, 0x37F, C.byref(sa))
    _check(winsta, "CreateWindowStation")
    buf = C.create_unicode_buffer(256)
    _check(GetUserObjectInformationW(winsta, 2, buf, C.sizeof(buf), None), "window station name")
    try:
        _check(SetProcessWindowStation(winsta), "SetProcessWindowStation")
        desk = CreateDesktopW(name, None, None, 0, 0x10000000, C.byref(sa))  # GENERIC_ALL
        _check(desk, "CreateDesktop")
    finally:
        SetProcessWindowStation(old)
        LocalFree(sd)
    return winsta, desk, buf.value + "\\" + name


# Job object ---------------------------------------------------------------------------


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
    _check(SetInformationJobObject(job, 9, C.byref(info), C.sizeof(info)), "job limits")
    rate = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(
        0x1 | 0x4, max(1, int(cpu_percent_of_one_core * 100 / os.cpu_count()))
    )
    _check(SetInformationJobObject(job, 15, C.byref(rate), C.sizeof(rate)), "job CPU cap")
    ui = DWORD(0xFF)  # all JOB_OBJECT_UILIMIT_* flags
    _check(SetInformationJobObject(job, 4, C.byref(ui), C.sizeof(ui)), "job UI restrictions")
    return job


def job_cpu_seconds(job: HANDLE) -> float:
    acc = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    _check(QueryInformationJobObject(job, 1, C.byref(acc), C.sizeof(acc), None), "job accounting")
    return (acc.TotalUserTime + acc.TotalKernelTime) / 1e7


def job_peak_mb(job: HANDLE) -> float:
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    _check(QueryInformationJobObject(job, 9, C.byref(info), C.sizeof(info), None), "job info")
    return info.PeakProcessMemoryUsed / 2**20


# Starting the worker ------------------------------------------------------------------


def env_block(env: dict[str, str]) -> C.Array:
    text = (
        "".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())) + "\0"
    )
    return C.create_unicode_buffer(text, len(text))


@dataclass
class Worker:
    pid: int
    process: HANDLE
    job: HANDLE
    stdin: object  # binary file, parent -> worker
    stdout: object  # binary file, worker -> parent
    desktop: tuple = ()  # private window station and desktop handles, if any

    def alive(self) -> bool:
        code = DWORD()
        GetExitCodeProcess(self.process, C.byref(code))
        return code.value == STILL_ACTIVE

    def wait(self, ms: int) -> bool:
        return WaitForSingleObject(self.process, ms) == 0

    def exit_code(self) -> int:
        code = DWORD()
        GetExitCodeProcess(self.process, C.byref(code))
        return code.value

    def kill(self) -> None:
        TerminateJobObject(self.job, 1)

    def close(self) -> None:
        for f in (self.stdin, self.stdout):
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
    env: dict[str, str] | None,
    cwd: str,
    stderr_path: str,
    container_sid: PVOID | None = None,
    disable_sids: list[str] | None = None,
    memory_mb: int = 512,
    cpu_percent_of_one_core: float = 50,
    private_desktop: bool = False,
) -> Worker:
    job = make_job(memory_mb, cpu_percent_of_one_core)
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    err_f = open(stderr_path, "ab")  # noqa: SIM115 (closed once the child has its handle)
    child = [msvcrt.get_osfhandle(fd) for fd in (in_r, out_w)] + [
        msvcrt.get_osfhandle(err_f.fileno())
    ]
    for h in child:
        os.set_handle_inheritable(h, True)

    si = STARTUPINFOEXW()
    si.StartupInfo.cb = C.sizeof(si)
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
    desktop_handles = ()
    if private_desktop:
        sid_text = sid_to_string(container_sid) if identity == "appcontainer" else None
        winsta, desk, desk_name = make_private_desktop(
            f"OpenChartWorker{next(_desktops)}", sid_text
        )
        desktop_handles = (winsta, desk, desk_name)
        desktop_buf = C.create_unicode_buffer(desk_name)
        si.StartupInfo.lpDesktop = C.cast(desktop_buf, W.LPWSTR)
    si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = child
    count = 2 if identity == "appcontainer" else 1
    size = C.c_size_t()
    InitializeProcThreadAttributeList(None, count, 0, C.byref(size))
    attrs = C.create_string_buffer(size.value)
    _check(
        InitializeProcThreadAttributeList(C.byref(attrs), count, 0, C.byref(size)), "attribute list"
    )
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
    caps = None
    if identity == "appcontainer":
        caps = SECURITY_CAPABILITIES(container_sid, None, 0, 0)
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
    )
    block = None
    if env is not None:
        block = C.byref(_env_block_buf := env_block(env))
        flags |= CREATE_UNICODE_ENVIRONMENT
    cmd = C.create_unicode_buffer(subprocess.list2cmdline(argv))
    pi = PROCESS_INFORMATION()
    try:
        if identity in ("restricted", "restricted-nolow"):
            # "restricted-nolow" keeps the parent's integrity level: PyInstaller's loader
            # cannot open its own archive at Low integrity (found in CI-0)
            token = restricted_token(disable_sids or [], low=identity == "restricted")
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
                        block,
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
                    None, cmd, None, None, True, flags, block, cwd, C.byref(si), C.byref(pi)
                ),
                "CreateProcess",
            )
        try:
            _check(AssignProcessToJobObject(job, pi.hProcess), "AssignProcessToJobObject")
        except OSError:
            TerminateProcess(pi.hProcess, 1)
            raise
        ResumeThread(pi.hThread)
        CloseHandle(pi.hThread)
    finally:
        DeleteProcThreadAttributeList(C.byref(attrs))
        os.close(in_r)
        os.close(out_w)
        err_f.close()
    return Worker(
        pi.dwProcessId,
        pi.hProcess,
        job,
        os.fdopen(in_w, "wb", buffering=0),
        os.fdopen(out_r, "rb", buffering=0),
        desktop_handles,
    )


def pid_alive(pid: int) -> bool:
    h = OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = DWORD()
    GetExitCodeProcess(h, C.byref(code))
    CloseHandle(h)
    return code.value == STILL_ACTIVE
