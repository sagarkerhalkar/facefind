"""Change ONLY the mail settings (keeps everything else). Run: mail.bat"""
import sys
from pathlib import Path

import setup

ENV = Path(__file__).resolve().parent / ".env"


def main():
    if not ENV.exists():
        print("Run start.bat first - it asks all the setup questions.")
        return
    values = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    print("=" * 60)
    print(" Mail setup - how the OTP codes are sent")
    print("=" * 60)
    values.update(setup.ask_mail())
    ENV.write_text("# Written by setup - run setup.bat or mail.bat to change.\n" +
                   "".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    print("\nSaved. Close the app window (if open) and run start.bat again.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled - nothing was changed.")
        sys.exit(1)
