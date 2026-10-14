"""Print a CI-0 report (JSON from run_spike.py) as a table: one column per mode."""

import json
import sys
from pathlib import Path

reports = [json.loads(Path(p).read_text(encoding="utf-8")) for p in sys.argv[1:]]
modes = [m for r in reports for m in r["modes"]]
SHORT = {"allowed": "ALLOWED", "denied": "denied", "timed out": "timed out", "error": "error"}

rows = list(dict.fromkeys(k for m in modes for k in m.get("probes", {})))
print("| probe | " + " | ".join(m["mode"] for m in modes) + " |")
print("|---|" + "---|" * len(modes))
for row in rows:
    cells = []
    for m in modes:
        p = m.get("probes", {}).get(row)
        if p is None:
            cells.append("-")
        elif row == "identity":
            cells.append(p["detail"].split(", ", 1)[1])
        elif row == "environment":
            cells.append(p["result"])
        else:
            cells.append(SHORT.get(p["result"], p["result"]))
    print(f"| {row} | " + " | ".join(cells) + " |")
for key in (
    "start",
    "start_s",
    "import_s",
    "python",
    "compute_5200_bars_ms",
    "cpu_cap",
    "memory",
    "timeout_hang",
    "restart_after_hang_s",
    "timeout_sleep",
    "restart_after_sleep_s",
    "parent_dies",
):
    print(f"| {key} | " + " | ".join(str(m.get(key, "-")) for m in modes) + " |")
for r in reports:
    print("cleanup:", "; ".join(r["cleanup"]))
for m in modes:
    for k, p in m.get("probes", {}).items():
        if p["result"] in ("error", "timed out"):
            print(f"{m['mode']} / {k}: {p['detail'][:160]}")
