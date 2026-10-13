# PyInstaller spec: the OpenChart server as a folder (openchart\openchart.exe plus
# _internal\), so a Windows PC needs no Python. Built by packaging/build-windows.ps1.
# The web UI must be built first (frontend: npm run build -> src/openchart/web).

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
SRC = os.path.join(ROOT, "src")
LICENSES = os.environ.get("OPENCHART_LICENSES_DIR", os.path.join(ROOT, "dist", ".build", "licenses", "server"))

if not os.path.isfile(os.path.join(SRC, "openchart", "web", "index.html")):
    raise SystemExit("Build the web UI first: cd frontend && npm ci && npm run build")

datas = []
# The web UI and the SQL migrations (read from disk at run time).
datas += collect_data_files("openchart")
# Indicator files are loaded by path (see openchart/indicators/_registry.py): ship them as files.
datas += [(os.path.join(SRC, "openchart", "indicators", "*.py"), os.path.join("openchart", "indicators"))]
datas += collect_data_files("tzdata")
datas += copy_metadata("keyring")  # keyring finds its backends through entry points
for name in ("LICENSE", "NOTICE"):
    datas.append((os.path.join(ROOT, name), "."))
datas.append((os.path.join(LICENSES, "THIRD-PARTY-LICENSES.txt"), "."))

hiddenimports = (
    collect_submodules("openchart")
    + collect_submodules("uvicorn")
    + collect_submodules("keyring.backends")
    + ["aiosqlite", "argon2", "_argon2_cffi_bindings"]
)

a = Analysis(
    [os.path.join(SPECPATH, "server_entry.py")],
    pathex=[SRC],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "webview"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="openchart",
    console=True,
    icon=os.path.join(ROOT, "packaging", "windows", "openchart.ico"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="openchart", upx=False)
