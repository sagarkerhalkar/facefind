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


def startup_file(folder=None):
    """Fallback that needs no Administrator rights: a small file in the Windows Startup
    folder starts FaceFind when this user signs in."""
    folder = folder or os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                                    "Start Menu", "Programs", "Startup")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "FaceFind.bat")
    with open(path, "w", newline="") as f:
        f.write(f'@echo off\r\ncd /d "{config.BASE}"\r\nstart "FaceFind" /min cmd /c run-server.bat\r\n')
    return path


def elevate(port):
    """Asks Windows for Administrator rights (the user sees a Yes/No box) and runs this
    file again with them. True if the box was accepted."""
    args = f'"{os.path.abspath(__file__)}" --elevated {port}'
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args,
                                               str(config.BASE), 1) > 32


def install(port=None, elevated=False):
    """True when FaceFind is set to start by itself. Prints what it did."""
    if os.name != "nt":
        print("  Automatic start is for Windows. On Linux use a systemd service that runs start.sh.")
        return False
    port = str(port or config.PORT)
    if not is_admin():
        print("  Windows will ask for permission (Administrator) - press Yes.")
        if elevate(port):
            print("  Done in the other window. FaceFind will then start by itself with Windows.")
            return True
        path = startup_file()
        print("  Permission was not given, so FaceFind will start when you sign in to Windows instead")
        print("  (" + path + "). For a server, turn on automatic sign-in or run install-service.bat again.")
        return True
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
    base = str(config.BASE).replace("'", "''")
    ps = (
        f"$a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c \"\"{base}\\run-server.bat\"\"' -WorkingDirectory '{base}'; "
        "$t = New-ScheduledTaskTrigger -AtStartup; $t.Delay = 'PT30S'; "
        "$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 "
        "-RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew; "
        "$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest; "
        f"Register-ScheduledTask -TaskName '{TASK}' -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       capture_output=True, text=True)
    if r.returncode:
        ok = False
        print("  Could not create the start-up task:", (r.stdout or r.stderr).strip())
    else:
        print("  FaceFind will now start by itself 30 seconds after Windows starts (also after a "
              "restart or a shut down), with no one signed in, and starts again if it ever stops.")
        print("  Its messages are kept in data\\service.log.")
    return ok


def remove():
    r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK], capture_output=True, text=True)
    print("  Start-up task removed." if not r.returncode else "  " + (r.stdout or r.stderr).strip())


if __name__ == "__main__":
    if "--remove" in sys.argv:
        remove()
        sys.exit(0)
    elevated = "--elevated" in sys.argv
    nums = [a for a in sys.argv[1:] if a.isdigit()]
    good = install(nums[0] if nums else None, elevated)
    if elevated or not good:
        input("\nPress Enter to close this window.")
    sys.exit(0 if good else 1)
