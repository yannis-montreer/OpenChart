"""Entry point of the frozen server (openchart.exe in the release zip): the same command
line as the `openchart` command of a pip install."""

import multiprocessing
import sys

from openchart.main import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
