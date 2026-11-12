"""Question-and-answer setup. Writes the .env settings file for you.
Run again any time to change the settings:  python setup.py"""
import getpass
import smtplib
import ssl
import sys
from pathlib import Path

import netutil

ENV = Path(__file__).resolve().parent / ".env"
MIN_PASSWORD = 6


def ask(question, default=""):
    hint = f" [{default}]" if default else ""
    answer = input(f"{question}{hint}: ").strip()
    return answer or default


def ask_hidden(question):
    if sys.stdin.isatty():
        return getpass.getpass(f"{question} (typing stays hidden): ").strip()
    return input(f"{question}: ").strip()


def ask_choice(question, options):
    print(f"\n{question}")
    for i, text in enumerate(options, 1):
        print(f"  {i}. {text}")
    while True:
        a = input(f"Type a number 1-{len(options)}: ").strip()
        if a.isdigit() and 1 <= int(a) <= len(options):
            return int(a)


def ask_yes(question, default=True):
    a = input(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not a else a.startswith("y")


def ask_password():
    while True:
        p = ask_hidden(f"Admin password (at least {MIN_PASSWORD} characters, no spaces)")
        if len(p) < MIN_PASSWORD or " " in p or '"' in p or "'" in p:
            print(f"  Please use {MIN_PASSWORD} or more characters, without spaces or quotes.")
            continue
        if ask_hidden("Type it once more") != p:
            print("  The two did not match. Try again.")
            continue
        return p


def test_mail(host, port, user, password):
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls(context=ctx)
            s.login(user, password)
        return ""
    except smtplib.SMTPAuthenticationError:
        return "The mail id or app password was not accepted."
    except Exception as e:
        return f"Could not reach the mail server ({e})."


def ask_mail():
    kind = ask_choice("Which mail id will send the OTP codes?", [
        "Gmail or Google Workspace",
        "Outlook / Office 365",
        "Another mail server",
        "Set up mail later (testing only - the code is shown in this window)",
    ])
    if kind == 4:
        return dict(SMTP_HOST="", SMTP_PORT="587", SMTP_USER="", SMTP_PASSWORD="",
                    MAIL_FROM="", DEV_MODE="1")
    host = {1: "smtp.gmail.com", 2: "smtp.office365.com"}.get(kind) or ask("Mail server (SMTP host)")
    port = "587" if kind != 3 else ask("Mail server port", "587")
    if kind == 1:
        print("\n  Gmail needs an APP PASSWORD, not your normal password:")
        print("  1. Open https://myaccount.google.com/apppasswords")
        print("     (2-Step Verification must be on)")
        print("  2. Create one named FaceFind and copy the 16 letters.\n")
    while True:
        user = ask("Mail id (e.g. you@gmail.com)")
        password = input("App password / mail password: ").replace(" ", "")
        if not user or not password:
            continue
        print("  Checking the mail login ...")
        problem = test_mail(host, int(port), user, password)
        if not problem:
            print("  Mail login works.")
            break
        print("  " + problem)
        if not ask_yes("Type the mail details again?"):
            print("  Saved anyway - OTP mails will fail until this is corrected.")
            break
    return dict(SMTP_HOST=host, SMTP_PORT=port, SMTP_USER=user, SMTP_PASSWORD=password,
                MAIL_FROM=user, DEV_MODE="0")


def ask_port():
    while True:
        a = ask("\nPort to use", "8443")
        if not a.isdigit() or not 1024 <= int(a) <= 65000:
            print("  Type a number between 1024 and 65000.")
            continue
        free = netutil.free_port("0.0.0.0", int(a))
        if free != int(a):
            if not ask_yes(f"  Port {a} is busy. Use {free} instead?"):
                continue
        return str(free)


def ask_networks(label):
    while True:
        a = ask(label + "\n  e.g. 192.168.10.0/24,192.168.20.0/24  (Enter = no limit)")
        try:
            netutil.parse_networks(a)
            return a
        except ValueError:
            print("  That is not a network like 192.168.10.0/24. Try again.")


def main():
    print("=" * 60)
    print(" FaceFind setup - answer the questions, press Enter after each")
    print("=" * 60)
    if ENV.exists() and not ask_yes("Settings already exist. Replace them?", False):
        return
    v = {"BRAND": ask("\nYour studio / business name", "My Studio")}
    v["ADMIN_PASSWORD"] = ask_password()
    v.update(ask_mail())
    v["HOST"] = "0.0.0.0"
    v["PORT"] = ask_port()
    v["HTTPS"] = "1"
    print("\nThis computer's addresses:", ", ".join(netutil.local_ips()) or "none found")
    v["ALLOWED_NETWORKS"] = v["ADMIN_NETWORKS"] = ""
    if ask_yes("Limit which networks / VLANs can open the app?", False):
        v["ALLOWED_NETWORKS"] = ask_networks("Networks allowed to open the app")
        v["ADMIN_NETWORKS"] = ask_networks("Networks allowed to open the ADMIN page")
    v.update(PUBLIC_URL="", TLS_NAMES="", TRUST_PROXY="0", MATCH_THRESHOLD="0.40",
             SESSION_HOURS="2")
    ENV.write_text("# Written by setup.py - run 'python setup.py' to change.\n" +
                   "".join(f"{k}={val}\n" for k, val in v.items()), encoding="utf-8")
    print("\nSaved. Starting the app ...")
    print(f"Admin page: https://{netutil.primary_ip()}:{v['PORT']}/admin\n")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled - nothing was saved.")
        sys.exit(1)
