"""Checks the mail settings and sends a real test mail to yourself.
Run:  testmail.bat   (or: python testmail.py)"""
import re
import smtplib
import socket
import ssl
from email.message import EmailMessage

import config

ENV = config.BASE / ".env"


def attempt(port):
    """Log in and send one test mail. Returns '' when it worked, else why not."""
    msg = EmailMessage()
    msg["Subject"] = f"{config.BRAND}: test mail works"
    msg["From"] = config.MAIL_FROM or config.SMTP_USER
    msg["To"] = config.SMTP_USER
    msg.set_content("If you can read this, the OTP mails will be delivered too.")
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            s = smtplib.SMTP_SSL(config.SMTP_HOST, 465, context=ctx, timeout=20)
        else:
            s = smtplib.SMTP(config.SMTP_HOST, port, timeout=20)
            s.starttls(context=ctx)
        with s:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)
        return ""
    except smtplib.SMTPAuthenticationError as e:
        return f"LOGIN REFUSED: {e.smtp_code} {e.smtp_error.decode('utf-8', 'replace')[:200]}"
    except (socket.timeout, TimeoutError, ConnectionError, socket.gaierror) as e:
        return f"BLOCKED: cannot connect to {config.SMTP_HOST} port {port} ({e})"
    except ssl.SSLError as e:
        return f"BLOCKED: secure connection was broken on port {port} ({e})"
    except Exception as e:
        return f"FAILED: {type(e).__name__}: {e}"


def main():
    print("Mail settings in use")
    print("  server  :", config.SMTP_HOST or "(none)")
    print("  mail id :", config.SMTP_USER or "(none)")
    print("  password:", f"{len(config.SMTP_PASSWORD)} characters" if config.SMTP_PASSWORD else "(none)")
    print("  test mode (DEV_MODE):", "ON" if config.DEV_MODE else "off")
    if not config.SMTP_HOST or not config.SMTP_USER or not config.SMTP_PASSWORD:
        print("\nMail is NOT set up. Run setup.bat and choose 1 (Gmail) at the mail question.")
        return
    if "gmail" in config.SMTP_HOST and len(config.SMTP_PASSWORD) != 16:
        print("\nNote: a Gmail app password has exactly 16 letters. Yours has "
              f"{len(config.SMTP_PASSWORD)} - it may be your normal password.")
    ports = [config.SMTP_PORT] + [p for p in (587, 465) if p != config.SMTP_PORT]
    results = {}
    for port in ports:
        print(f"\nTrying port {port} ...")
        results[port] = attempt(port)
        if not results[port]:
            print(f"  WORKS. A test mail was sent to {config.SMTP_USER} - check the inbox (and Spam).")
            if port != config.SMTP_PORT:
                text = ENV.read_text(encoding="utf-8")
                text, n = re.subn(r"(?m)^SMTP_PORT=.*$", f"SMTP_PORT={port}", text)
                if not n:
                    text += f"\nSMTP_PORT={port}\n"
                ENV.write_text(text, encoding="utf-8")
                print(f"  Port {config.SMTP_PORT} did not work, so the app now uses port {port}.")
            print("\nClose the app window and run start.bat again.")
            return
        print("  " + results[port])
    print("\nMail does not work yet.")
    if any(r.startswith("LOGIN REFUSED") for r in results.values()):
        print("Google/your mail server refused the mail id or password. Create a NEW app")
        print("password, then run setup.bat and paste it at the mail question.")
    else:
        print("Your network, firewall or antivirus is blocking outgoing mail. Try once on a")
        print("mobile hotspot, or switch off the antivirus 'mail shield' and run this again.")
    print("Copy the text above and send it to the developer if it is not clear.")


if __name__ == "__main__":
    main()
