"""Keeps FaceFind up on a Windows server. install_service.py runs this every
5 minutes. If the app does not answer two checks in a row (so it has been down
for 5 to 10 minutes), it is stopped and started again. Messages go to
data\\service.log.   Run by hand:  python watchdog.py
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

import config
from install_service import TASK

STATE = config.DATA_DIR / "watchdog.json"
LOG = config.DATA_DIR / "service.log"
FAILS_BEFORE_RESTART = 2


def port():
    try:
        return int((config.DATA_DIR / "port.txt").read_text().strip())
    except (OSError, ValueError):
        return config.PORT


def answers(p, timeout=20):
    """True when the app replies on its port (any HTTP reply counts, even an error page)."""
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{p}/", timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def log(msg):
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " watchdog: " + msg + "\n")
    except OSError:
        pass


def load_fails():
    try:
        return int(json.loads(STATE.read_text()).get("fails", 0))
    except (OSError, ValueError, AttributeError):
        return 0


def save_fails(n):
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"fails": n, "at": int(time.time())}))
    except OSError:
        pass


def restart(p):
    """Stops whatever holds the port (a stuck app) and starts the start-up task.
    The task's run-server.bat also restarts the app by itself once it is stopped."""
    ps = (f"Get-NetTCPConnection -LocalPort {int(p)} -State Listen -ErrorAction SilentlyContinue | "
          "ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                   capture_output=True, text=True)
    time.sleep(3)
    r = subprocess.run(["schtasks", "/Run", "/TN", TASK], capture_output=True, text=True)
    return r.returncode == 0


def check(p=None, ok=answers, do_restart=restart):
    """One check. Returns what it did: 'ok', 'waiting' or 'restarted'."""
    p = p or port()
    if ok(p):
        if load_fails():
            log(f"port {p} answers again")
        save_fails(0)
        return "ok"
    fails = load_fails() + 1
    if fails < FAILS_BEFORE_RESTART:
        save_fails(fails)
        log(f"port {p} does not answer (check {fails}); will restart if it is still down next time")
        return "waiting"
    started = do_restart(p)
    log(f"port {p} still does not answer - restarted FaceFind" + ("" if started else " (could not start the task)"))
    save_fails(0)
    return "restarted"


if __name__ == "__main__":
    print(check())
    sys.exit(0)
