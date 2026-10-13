"""
watchdog.py — keeps RDC Electricity Intelligence (website + 2-hourly scheduler) alive, the same way RDC-OPS's watchdog keeps RDC-OPS alive.

    .venv\\Scripts\\python watchdog.py          (or double-click START_EB.bat)

It starts app.py as a child process (the web app starts the scan scheduler and the bill-reading worker inside it). If the process exits for
any reason it is restarted after a short pause; the scheduler resumes from the schedule stored in the database, so a restart never skips or
repeats work. Put a shortcut to START_EB.bat in the Windows Startup folder to bring it up after a reboot.
"""

import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(BASE_DIR, "app.py")
PYTHON = sys.executable
LOG = os.path.join(BASE_DIR, "data", "server.log")
HOST, PORT = os.environ.get("EB_HOST", "127.0.0.1"), os.environ.get("EB_PORT", "2002")
HEALTH_URL = f"http://{'127.0.0.1' if HOST in ('0.0.0.0', '') else HOST}:{PORT}/healthz"
HEALTH_EVERY, HEALTH_FAILS_TO_RESTART = 60, 3


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def healthy() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def main() -> None:
    log("Watchdog started.")
    while True:
        log("Starting app.py ...")
        proc = None
        try:
            proc = subprocess.Popen([PYTHON, APP], cwd=BASE_DIR)
            fails, started = 0, time.time()
            while proc.poll() is None:
                time.sleep(HEALTH_EVERY)
                if time.time() - started < 90:                 # give the server time to start
                    continue
                fails = 0 if healthy() else fails + 1
                if fails >= HEALTH_FAILS_TO_RESTART:
                    log(f"The web server did not answer {fails} times in a row; restarting it.")
                    proc.terminate()
                    try:
                        proc.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
            log(f"app.py stopped (exit code {proc.returncode}). Restarting in 3 s ...")
        except KeyboardInterrupt:
            log("Watchdog stopped by the user.")
            if proc and proc.poll() is None:
                proc.terminate()
            sys.exit(0)
        except Exception as exc:
            log(f"Watchdog error: {exc}. Retrying in 5 s ...")
            time.sleep(5)
            continue
        time.sleep(3)


if __name__ == "__main__":
    main()
