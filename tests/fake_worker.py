"""A stand-in for the indicator worker: the real protocol, no pandas, so it starts in a
fraction of a second (tests/test_worker_runner.py).

What a "script" does is written in its source: `HANG` loops, `SLEEP` sleeps, `CRASH`
ends the process, `RAISE` raises; anything else describes one output and echoes the
closes.
"""

import os
import site
import sys
import time

for i, arg in enumerate(sys.argv):
    if arg == "--site":
        site.addsitedir(sys.argv[i + 1])
sys.path.insert(0, sys.argv[sys.argv.index("--path") + 1])

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
            head = {"probes": {"environment": {"result": "clean", "detail": ""}}}
        else:
            if req.get("source") is not None:
                sources[req["hash"]] = req["source"]
            source = sources.get(req.get("hash"))
            if source is None:
                head = {"error": {"type": "NeedSource", "message": "send the source"}}
            elif cmd == "describe":
                head = {"definition": DEFINITION}
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
        fout.write(encode(head, out))
        fout.flush()


main()
