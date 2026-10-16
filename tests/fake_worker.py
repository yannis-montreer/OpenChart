"""A stand-in for the indicator worker: the real protocol, no pandas, so it starts in a
fraction of a second (tests/test_worker_runner.py).

What a "script" does is written in its source: `HANG` loops, `SLEEP` sleeps, `CRASH`
ends the process, `RAISE` raises, `SLOWRAISE` raises after 0.4 s, `PAUSEMS <n>` waits n ms
and answers, `STALLBELOW <n>` loops and `ENDBELOW <n>` ends the process on fewer than n
bars, `SHIFT` answers the next bar's
close (lookahead), `FILL <folder>` and
`FILLIDLE <folder>` fill a folder (during the job, or from a thread after it); anything
else describes one output and echoes the closes.
"""

import os
import site
import sys
import threading
import time

for i, arg in enumerate(sys.argv):
    if arg == "--site":
        site.addsitedir(sys.argv[i + 1])
sys.path.insert(0, sys.argv[sys.argv.index("--path") + 1])

import numpy as np  # noqa: E402 (the protocol needs it anyway)

from openchart.indicators.worker.protocol import encode, exact_reader, read_frame  # noqa: E402

DEFINITION = {
    "id": "fake",
    "name": "Fake",
    "title": "Fake",
    "overlay": True,
    "params": [
        {
            "name": "color",
            "type": "color",
            "default": "#2962ff",
            "label": "Color",
            "group": "style",
        },
        {
            "name": "width",
            "type": "width",
            "default": 1,
            "label": "Width",
            "group": "style",
            "min": 1,
            "max": 4,
            "step": 1,
        },
    ],
    "outputs": [{"name": "out", "label": "Out", "color": "color", "width": "width"}],
    "builtin": False,
}


DEFINITION_MARKERS = {
    **DEFINITION,
    "outputs": [
        {
            "kind": "line",
            "name": "out",
            "label": "Out",
            "color": "color",
            "width": "width",
            "style": "solid",
        },
        {
            "kind": "marker",
            "name": "hit",
            "label": "Hit",
            "color": "color",
            "shape": "circle",
            "position": "above",
            "text": "",
        },
    ],
}


def bad_result(kind, head, out, arrays):
    """What a worker that cannot be trusted might answer (`BAD <kind>` in the source)."""
    n = len(arrays["c"])
    nan = np.full(n, np.nan)
    if kind == "undeclared":
        return {"outputs": ["out", "hit", "zzz"]}, {**out, "zzz": arrays["c"]}
    if kind == "unlisted":
        return head, {**out, "zzz": arrays["c"]}
    if kind == "missing":
        return {"outputs": ["out"]}, {"out": arrays["c"]}
    if kind == "none":
        return {"outputs": []}, {}
    if kind == "notalist":
        return {"outputs": "out"}, out
    if kind == "notnames":
        return {"outputs": [1, 2]}, out
    if kind == "ghost":
        return {"outputs": ["out", "hit", "ghost"]}, out
    if kind == "nooutputs":
        return {}, out
    if kind == "duplicate":
        return {"outputs": ["out", "out", "hit"]}, out
    if kind == "many":
        names = [f"o{i}" for i in range(40)]
        return {"outputs": names}, {k: arrays["c"] for k in names}
    if kind == "short":
        return head, {**out, "out": arrays["c"][:-1]}
    if kind == "long":
        return head, {**out, "out": np.concatenate([arrays["c"], arrays["c"]])}
    if kind == "huge":
        return head, {**out, "out": np.zeros(3_000_000)}
    if kind == "empty":
        return head, {**out, "out": np.zeros(0)}
    if kind == "marker2":
        return head, {**out, "hit": arrays["c"] * 0 + 2}
    if kind == "markernan":
        return head, {**out, "hit": nan}
    if kind == "nanonly":
        return head, {**out, "out": nan}
    if kind == "inf":
        return head, {**out, "out": np.full(n, np.inf)}
    raise SystemExit(f"unknown BAD kind {kind}")


def main():
    fin, fout = sys.stdin.buffer, sys.stdout.buffer
    read = exact_reader(fin)
    fout.write(encode({"ready": True, "pid": os.getpid(), "python": "fake"}))
    fout.flush()
    sources = {}
    while True:
        try:
            req, arrays = read_frame(read)
        except EOFError:
            return
        cmd = req.get("cmd")
        if cmd == "exit":
            return
        head, out = {}, {}
        if cmd == "arm":
            head = {"armed": True}
        elif cmd == "probe":
            # A plain process reads whatever it is asked to: "allowed".
            probes = {"environment": {"result": "clean", "detail": ""}}
            for label in (req.get("targets") or {}).get("read") or {}:
                probes[label] = {"result": "allowed", "detail": "fake"}
            head = {"probes": probes}
        else:
            if req.get("source") is not None:
                sources[req["hash"]] = req["source"]
            source = sources.get(req.get("hash"))
            if source is None:
                head = {"error": {"type": "NeedSource", "message": "send the source"}}
            elif cmd == "describe":
                head = {"definition": DEFINITION_MARKERS if "MARKERS" in source else DEFINITION}
            elif cmd == "warmup":
                head = {"warmup": 0}
            else:
                if "HANG" in source:
                    while True:
                        pass
                if "SLEEP" in source:
                    time.sleep(3600)
                if "CRASH" in source:
                    os._exit(3)
                if source.startswith(("FILL ", "FILLIDLE ")):  # write 1 MB a step into a folder
                    folder = source.split(" ", 1)[1].strip()

                    def fill(folder=folder):
                        for i in range(10_000):
                            with open(os.path.join(folder, f"f{i}.bin"), "wb") as f:
                                f.write(b"x" * 2**20)
                            time.sleep(0.02)

                    if source.startswith("FILL "):
                        fill()
                    else:  # after the answer, from a thread: the job itself is done
                        threading.Thread(target=fill, daemon=True).start()
                if "SLOWRAISE" in source:
                    time.sleep(0.4)
                if "PAUSEMS " in source:
                    time.sleep(int(source.split("PAUSEMS ", 1)[1].split()[0]) / 1000)
                if "STALLBELOW " in source and len(arrays["c"]) < int(
                    source.split("STALLBELOW ", 1)[1].split()[0]
                ):  # loops on fewer bars: a check's shorter run
                    while True:
                        pass
                if "ENDBELOW " in source and len(arrays["c"]) < int(
                    source.split("ENDBELOW ", 1)[1].split()[0]
                ):  # ends the process on fewer bars
                    os._exit(3)
                if "RAISE" in source:
                    head = {
                        "error": {
                            "type": "ZeroDivisionError",
                            "message": "division by zero",
                            "line": 7,
                            "traceback": "",
                        }
                    }
                else:
                    head = {"outputs": ["out"]}
                    out = {"out": arrays["c"]}
                    if "SHIFT" in source:  # the next bar's close: lookahead
                        out = {"out": np.append(arrays["c"][1:], np.nan)}
                    if "MARKERS" in source:
                        head, out = (
                            {"outputs": ["out", "hit"]},
                            {"out": arrays["c"], "hit": arrays["c"] * 0},
                        )
                    bad = source.split("BAD ", 1)[1].split()[0] if "BAD " in source else ""
                    if bad:
                        head, out = bad_result(bad, head, out, arrays)
        fout.write(encode(head, out))
        fout.flush()


main()
