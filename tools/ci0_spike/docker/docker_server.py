"""CI-0 spike, Docker: stands in for the server. It holds a decoy secret in its environment
and a decoy database in /data, talks to the sidecar over the shared Unix socket, and writes
/out/report.json. Exit code 0 only if the minimum isolation holds."""

from __future__ import annotations

import json
import os
import resource
import socket
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from worker_probe import recv, send

SOCK = "/run/ocw/sock"


def bars(n):
    rng = np.random.default_rng(7)
    c = 2000 + np.cumsum(rng.normal(0, 1, n))
    o = np.r_[c[0], c[:-1]]
    return {
        "ts": np.arange(n, dtype="f8") * 60,
        "o": o,
        "h": np.maximum(o, c) + 0.5,
        "l": np.minimum(o, c) - 0.5,
        "c": c,
        "v": np.ones(n),
    }


def main() -> None:
    os.makedirs("/data", exist_ok=True)
    with open("/data/openchart.db", "wb") as f:
        f.write(os.urandom(4096))
    srv = socket.create_server(("0.0.0.0", 8000))  # something for the worker to try to reach
    threading.Thread(
        target=lambda: [srv.accept()[0].close() for _ in iter(int, 1)], daemon=True
    ).start()

    for _ in range(100):
        if os.path.exists(SOCK):
            break
        time.sleep(0.2)
    s = socket.socket(socket.AF_UNIX)
    s.connect(SOCK)
    f = s.makefile("rwb", buffering=0)

    def ask(header, arrays=None):
        send(f, header, arrays)
        return recv(f)

    report: dict = {
        "server": {"uid": os.getuid(), "env_has_secret": "OPENCHART_SECRET_KEY" in os.environ}
    }
    report["probes"] = ask({"cmd": "probe"})[0]["probes"]

    b = bars(5200)
    rt, inner = [], []
    for _ in range(20):
        t = time.perf_counter()
        head, _out = ask({"cmd": "compute"}, b)
        rt.append((time.perf_counter() - t) * 1000)
        inner.append(head["compute_ms"])
    report["compute_5200_bars_ms"] = {
        "worker_median": round(statistics.median(inner), 1),
        "round_trip_median_incl_fork": round(statistics.median(rt), 1),
        "round_trip_max": round(max(rt), 1),
    }

    t = time.perf_counter()
    report["timeout_hang"] = ask({"cmd": "hang", "deadline": 1.0})[0]
    report["timeout_hang"]["answered_after_s"] = round(time.perf_counter() - t, 2)
    report["memory_with_rlimit"] = ask({"cmd": "alloc", "mb": 2048})[0]
    report["memory_cgroup_only"] = ask(
        {"cmd": "alloc", "mb": 2048, "limit_as": False, "deadline": 30}
    )[0]
    report["worker_still_answers"] = "compute_ms" in ask({"cmd": "compute"}, bars(300))[0]

    t = time.perf_counter()
    ask({"cmd": "cpu", "seconds": 4, "deadline": 30})
    wall = time.perf_counter() - t
    report["cpu"] = (
        f"limit cpus: 0.5; 4 s of CPU took {wall:.1f} s -> about {4 / wall * 100:.0f}% of one core"
    )
    send(f, {"cmd": "exit"})

    os.makedirs("/out", exist_ok=True)
    with open("/out/report.json", "w") as out_file:
        json.dump(report, out_file, indent=1)
    print(json.dumps(report, indent=1))

    p = report["probes"]
    must_fail = [
        "read the database (/data/openchart.db)",
        "read /data/secrets.enc",
        "connect to the server container (server:8000)",
        "connect out (192.0.2.1:9)",
        "write into /",
    ]
    ok = (
        p["environment"]["result"] == "clean"
        and all(p[k]["result"] != "allowed" for k in must_fail)
        and "timed out" in report["timeout_hang"].get("error", "")
        and report["worker_still_answers"]
    )
    print("MINIMUM ISOLATION:", "holds" if ok else "NOT met")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    main()
