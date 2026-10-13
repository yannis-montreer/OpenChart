# PyInstaller spec: OpenChart Viewer, one windowed .exe (pywebview, WebView2).
# Built by packaging/build-windows.ps1.

import os

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
LICENSES = os.environ.get("OPENCHART_LICENSES_DIR", os.path.join(ROOT, "dist", ".build", "licenses", "viewer"))

datas = [(os.path.join(ROOT, name), ".") for name in ("LICENSE", "NOTICE")]
datas.append((os.path.join(LICENSES, "THIRD-PARTY-LICENSES.txt"), "."))

a = Analysis(
    [os.path.join(ROOT, "packaging", "viewer", "openchart_viewer.py")],
    datas=datas,
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "pyarrow", "openchart"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="OpenChart-Viewer",
    console=False,
    icon=os.path.join(ROOT, "packaging", "windows", "openchart.ico"),
    upx=False,
)
