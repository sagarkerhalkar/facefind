"""Makes FaceFind start by itself when this Windows computer starts, and opens
its port for the office network. Run it once as Administrator:
install-service.bat   (setup.py offers to run it for you)
"""
import ctypes
import os
import subprocess
import sys

import config

TASK = "FaceFind"


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def install(port=None):
    """True when both steps worked. Prints what it did."""
    if os.name != "nt":
        print("  Automatic start is for Windows. On Linux use a systemd service that runs start.sh.")
        return False
    if not is_admin():
        print("  Needs Administrator rights. Right-click install-service.bat, Run as administrator.")
        return False
    port = str(port or config.PORT)
    ok = True
    r = subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=FaceFind"],
                       capture_output=True, text=True)
    r = subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule", "name=FaceFind", "dir=in",
                        "action=allow", "protocol=TCP", f"localport={port}", "profile=private,domain"],
                       capture_output=True, text=True)
    if r.returncode:
        ok = False
        print("  Could not open the port in the firewall:", (r.stdout or r.stderr).strip())
    else:
        print(f"  Firewall: port {port} is open for the office network.")
    cmd = f'cmd /c cd /d "{config.BASE}" && start.bat'
    r = subprocess.run(["schtasks", "/Create", "/F", "/TN", TASK, "/SC", "ONSTART", "/RU", "SYSTEM",
                        "/RL", "HIGHEST", "/TR", cmd], capture_output=True, text=True)
    if r.returncode:
        ok = False
        print("  Could not create the start-up task:", (r.stdout or r.stderr).strip())
    else:
        print("  FaceFind will now start by itself when this computer starts.")
    return ok


if __name__ == "__main__":
    sys.exit(0 if install() else 1)
