# ruff: noqa: E501 (a record of a one-off investigation, kept as it ran)
"""What can an AppContainer worker do in its own folder? (CI-1, finding 2, owner's B)

Runs once as the owner's user, under the owner's conditions and limits:
- a unique container name (OpenChart.Investigation.<random>), never the installer's;
- read and execute granted only on a throwaway copy of Python in this scratch folder;
- decoys only in this scratch folder; no Credential Manager or registry decoys; no network;
- every path checked before each step: inside the unique package folder, or (the
  junction's target) inside this scratch folder; anything else stops the run;
- 200 MB written at most, once, inside the container's own folder;
- cleanup: deny entry removed and verified, junction removed on its own (never a delete
  through it), decoy verified, then the profile; no force-delete, no ownership taken.

Usage: python investigate.py <scratch folder>   (PYTHONPATH = the worktree's src)
"""

import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import winreg
from pathlib import Path

from openchart.indicators.worker import windows

SCRATCH = Path(sys.argv[1]).resolve()
NAME = f"OpenChart.Investigation.{secrets.token_hex(4)}"
RT = SCRATCH / "rt"  # the throwaway Python copy
DECOY = SCRATCH / "decoy-outside"  # the junction's target
PACKAGES = Path(os.environ["LOCALAPPDATA"]).resolve() / "Packages"
MAPPINGS = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppContainer\Mappings"
LINKS = ("junction-to-decoy", "own-junction", "own-symlink")


class Stop(RuntimeError):
    """A path outside the allowed folders, or a cleanup step that did not hold."""


def inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        return False


def must_be_in(path: Path, root: Path, what: str) -> None:
    real = Path(os.path.realpath(path))
    if not (inside(real, root) and inside(Path(os.path.abspath(path)), root)):
        raise Stop(f"{what}: {path} resolves to {real}, outside {root}: not run")


PROBE = r"""
import json, os, sys
own, package, decoy, step = sys.argv[1:5]
out = {}
def t(name, fn):
    try:
        out[name] = "allowed: " + str(fn())
    except Exception as e:
        out[name] = f"refused: {type(e).__name__}: {e}"
def write_mb(n):
    done = 0
    for i in range(n):  # at most n MB, then stop
        with open(os.path.join(own, f"fill{i}.bin"), "wb") as f:
            f.write(b"x" * 2**20)
        done += 1
    return f"{done} MB written"
def write(path):
    with open(path, "w") as f:
        f.write("x")
    return "written"
if step == "first":
    t("write a file in its own folder", lambda: write(os.path.join(own, "marker.txt")))
    t("write 200 MB in its own folder (capped at 200)", lambda: write_mb(200))
    t("make a subfolder in its own folder", lambda: os.mkdir(os.path.join(own, "sub")) or "made")
    t("list its package folder (one level up)", lambda: len(os.listdir(package)))
    t("write into its package folder (one level up)", lambda: write(os.path.join(package, "up.txt")))
    t("read the decoy folder directly", lambda: os.listdir(decoy))
    t("read the decoy through the user-made junction", lambda: open(os.path.join(own, "junction-to-decoy", "secret.txt")).read())
    t("write through the user-made junction", lambda: write(os.path.join(own, "junction-to-decoy", "written.txt")))
    t("create a symlink in its folder to the decoy", lambda: os.symlink(decoy, os.path.join(own, "own-symlink")) or "made")
    t("start mklink /J (a program) to make its own junction", lambda: __import__("subprocess").run(["cmd", "/c", "mklink", "/J", os.path.join(own, "own-junction"), decoy]).returncode)
    t("TEMP points at", lambda: os.environ.get("TEMP"))
elif step == "second":
    t("read the marker left by worker 1", lambda: open(os.path.join(own, "marker.txt")).read())
    t("files left by worker 1", lambda: len(os.listdir(own)))
else:  # after the deny entry
    t("write a file in its own folder", lambda: write(os.path.join(own, "after-deny.txt")))
    t("write in its subfolder", lambda: write(os.path.join(own, "sub", "after-deny.txt")))
    t("delete a file in its own folder", lambda: os.remove(os.path.join(own, "marker.txt")) or "deleted")
print(json.dumps(out))
"""


def icacls(path: Path) -> str:
    return subprocess.run(
        ["icacls", str(path)], capture_output=True, text=True, errors="replace"
    ).stdout.strip()


def copy_python() -> Path:
    base = Path(sys.base_prefix)
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
    shutil.copytree(base / "Lib", RT / "Lib", ignore=skip)
    shutil.copytree(
        base / "DLLs", RT / "DLLs", ignore=shutil.ignore_patterns("tcl*", "tk*", "_tkinter*")
    )
    for f in base.iterdir():
        if f.is_file() and f.suffix.lower() in (".exe", ".dll"):
            shutil.copy2(f, RT / f.name)
    (RT / "probe.py").write_text(PROBE)
    return RT / "python.exe"


def run(python: Path, sid, own: Path, package: Path, step: str) -> dict:
    must_be_in(own, package, "its own folder")
    must_be_in(DECOY, SCRATCH, "the decoy")
    env = {
        "SYSTEMROOT": os.environ["SYSTEMROOT"],
        "PATH": str(RT),
        "LOCALAPPDATA": os.environ["LOCALAPPDATA"],
    }
    p = windows.start(
        [str(python), "-I", "-B", str(RT / "probe.py"), str(own), str(package), str(DECOY), step],
        identity="appcontainer",
        env=env,
        cwd=str(RT),
        container=sid,
        memory_mb=512,
    )
    try:
        p.wait(180_000)
        out = p.stdout.read().decode("utf-8", "replace")
        err = p.stderr.read().decode("utf-8", "replace")
    finally:
        p.close()
    try:
        return json.loads(out)
    except ValueError:
        return {"(no answer)": (out + err)[-500:]}


def main() -> None:
    report: dict = {"container": NAME}
    must_be_in(RT, SCRATCH, "the Python copy")
    must_be_in(DECOY, SCRATCH, "the decoy")
    DECOY.mkdir(parents=True, exist_ok=True)
    (DECOY / "secret.txt").write_text("decoy, not a secret")
    sid = sid_text = own = package = None
    try:
        python = copy_python()
        sid = windows.ensure_profile(NAME)
        sid_text = windows.sid_to_string(sid)
        package = PACKAGES / NAME.lower()
        if not package.is_dir():
            raise Stop(f"the package folder {package} is not where expected")
        own = Path(windows.profile_folder(sid_text))
        must_be_in(own, package, "its own folder")
        report["sid"] = sid_text
        report["own folder"] = str(own)
        g = subprocess.run(
            ["icacls", str(RT), "/grant", f"*{sid_text}:(OI)(CI)RX", "/T", "/C", "/Q"],
            capture_output=True,
            text=True,
        )
        report["grant on the Python copy (exit)"] = g.returncode
        report["own folder ACL, as created"] = icacls(own)
        report["package folder ACL"] = icacls(package)
        junction = own / "junction-to-decoy"
        must_be_in(junction.parent, package, "where the junction goes")
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(DECOY)], capture_output=True
        )
        if os.path.realpath(junction) != str(DECOY):
            raise Stop(f"the junction resolves to {os.path.realpath(junction)}, not the decoy")
        report["run 1"] = run(python, sid, own, package, "first")
        report["size of its folder after run 1 (MB)"] = round(
            sum(
                f.stat().st_size
                for f in own.rglob("*")
                if f.is_file() and not inside(Path(os.path.realpath(f)), SCRATCH)
            )
            / 2**20,
            1,
        )
        report["run 2 (a new worker)"] = run(python, sid, own, package, "second")
        must_be_in(own, package, "the deny entry's folder")
        # The deny entry CI-1's server first added (removed since: it does not hold).
        subprocess.run(
            ["icacls", str(own), "/deny", f"*{sid_text}:(OI)(CI)(W,D,DC)"], capture_output=True
        )
        report["own folder ACL, after the deny"] = icacls(own)
        report["run 3 (after the deny)"] = run(python, sid, own, package, "deny")
    except Stop as exc:
        report["STOPPED"] = str(exc)
    finally:
        report["cleanup"] = cleanup(sid_text, own, package)
    print(json.dumps(report, indent=1))


def cleanup(sid_text, own, package) -> dict:
    proof: dict = {}
    time.sleep(1)
    if sid_text and own and package and inside(own, package):
        # 1. The deny entry, removed and verified before anything is deleted.
        subprocess.run(["icacls", str(own), "/remove:d", f"*{sid_text}"], capture_output=True)
        left = [
            line for line in icacls(own).splitlines() if sid_text in line and "DENY" in line.upper()
        ]
        proof["deny entry removed"] = not left
        if left:
            proof["STOPPED"] = "the deny entry is still there: profile not deleted"
            return proof
        # 2. Links on their own, never a delete through them; then the decoy checked.
        for name in LINKS:
            p = own / name
            if p.is_junction() or p.is_symlink():
                must_be_in(p.parent, package, "a link to remove")
                if p.is_junction() or p.is_dir():
                    os.rmdir(p)  # removes the link itself
                else:
                    os.unlink(p)
        proof["links gone"] = not any(
            (own / n).is_junction() or (own / n).is_symlink() for n in LINKS
        )
        proof["decoy intact"] = (DECOY / "secret.txt").read_text() == "decoy, not a secret"
    # 3. The profile (the package folder with it).
    hr = windows.delete_profile(NAME)
    proof["DeleteAppContainerProfile HRESULT"] = hex(hr & 0xFFFFFFFF)
    found = 0
    with contextlib.suppress(OSError), winreg.OpenKey(winreg.HKEY_CURRENT_USER, MAPPINGS) as k:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(k, i)
            except OSError:
                break
            with winreg.OpenKey(k, sub) as s, contextlib.suppress(OSError):
                if winreg.QueryValueEx(s, "Moniker")[0].lower() == NAME.lower():
                    found += 1
            i += 1
    proof["registry mappings left"] = found
    proof["package folder exists"] = (PACKAGES / NAME.lower()).exists()
    ps = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"@(Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -like '{RT}*' }}).Count",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    proof["processes from the Python copy"] = ps
    # 4. Scratch: the copy and the decoy.
    shutil.rmtree(RT, ignore_errors=True)
    shutil.rmtree(DECOY, ignore_errors=True)
    proof["scratch copy and decoy exist"] = RT.exists() or DECOY.exists()
    return proof


main()
