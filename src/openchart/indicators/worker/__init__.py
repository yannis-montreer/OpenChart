"""The indicator worker: custom indicator code runs here, never in the server.

- `protocol.py`: the frames both sides speak;
- `child.py`: the worker itself (`openchart indicator-worker`);
- `runner.py`: the server's side (queue, deadlines, respawn);
- `isolation.py`: which worker an install gets, and the probes that say how isolated it is;
- `windows.py`, `posix.py`: starting it with less than the server can do.

See docs/custom-indicators.md for what it protects against and what it does not.
"""
