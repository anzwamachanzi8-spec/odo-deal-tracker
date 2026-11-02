"""Send everything the program prints to logs/tracker.log as well as the console.

The rest of the code just uses print(). Here we swap sys.stdout for a small object that
forwards each printed line to Python's logging module, which adds a timestamp and rotates the
file at 1 MB (keeping 5 old files), so the log can't fill the disk.

Under Task Scheduler we start pythonw.exe (no console window), where sys.__stdout__ is None,
so console output is skipped and only the log file is written.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger("odo")


class _PrintToLog:
    def __init__(self, console):
        self.console = console
        self._partial = ""

    def write(self, text: str):
        if self.console:
            self.console.write(text)
        # print() may send a line in pieces; only log complete lines.
        self._partial += text
        *lines, self._partial = self._partial.split("\n")
        for line in lines:
            if line.strip():
                logger.info(line)

    def flush(self):
        if self.console:
            self.console.flush()


def start_logging(log_dir: Path):
    handler = RotatingFileHandler(log_dir / "tracker.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    sys.stdout = _PrintToLog(sys.__stdout__)
