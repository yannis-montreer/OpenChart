"""Entry point of the frozen server (openchart.exe in the release zip): the same command
line as the `openchart` command of a pip install."""

import multiprocessing
import sys

if __name__ == "__main__":
    if sys.argv[1:2] == ["indicator-worker"]:
        # The indicator worker: it imports nothing of the server (no web, no broker).
        from openchart.indicators.worker.child import main as worker_main

        sys.exit(worker_main(sys.argv[2:]))
    multiprocessing.freeze_support()
    from openchart.main import main

    sys.exit(main())
