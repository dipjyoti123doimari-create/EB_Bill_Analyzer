"""
worker.py — run the scheduler without the website:   python -m eb_app.worker

The web app starts the same scheduler inside its own process by default; this entry point exists for a deployment where the scan must
keep running even when the website is stopped (start the website with EB_SCHEDULER=0 in that case). Running both at once is safe: the
database lock 'scan-cycle' lets only one of them scan at a time.
"""

import logging
import signal
import sys
import threading

from . import storage
from .oracle_source import OracleSource
from .scheduler import Scheduler


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    storage.init_db()
    source = OracleSource()
    sch = Scheduler(source)
    info = sch.start()
    logging.getLogger("eb.worker").warning("RDC-EB worker running (recovery: %s). Ctrl+C to stop.", info)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass
    try:
        stop.wait()
    finally:
        sch.stop()
        source.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
