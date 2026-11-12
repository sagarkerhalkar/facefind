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
    """Tries the login on the given port, then on the other usual one.
    Returns (problem, port): problem is '' when a port worked."""
    problem = ""
    for p in [port] + [x for x in (587, 465) if x != port]:
        try:
            ctx = ssl.create_default_context()
            if p == 465:
                s = smtplib.SMTP_SSL(host, 465, context=ctx, timeout=20)
            else:
                s = smtplib.SMTP(host, p, timeout=20)
                s.starttls(context=ctx)
            with s:
                s.login(user, password)
            return "", p
        except smtplib.SMTPAuthenticationError:
            return "The mail id or app password was not accepted.", port
        except Exception as e:
            problem = (f"The network closed the connection to the mail server ({e}). "
                       "Your firewall or antivirus blocks mail ports - choose Brevo (4) instead.")
    return problem, port


NO_MAIL = dict(SMTP_HOST="", SMTP_PORT="587", SMTP_USER="", SMTP_PASSWORD="",
               MAIL_FROM="", BREVO_API_KEY="", DEV_MODE="0")


def ask_brevo():
    print("\n  Brevo is a free mail service (300 mails a day). It works on office")
    print("  networks that block mail ports. Guests see the mail coming from YOUR mail id.")
    print("  1. Sign up free at https://www.brevo.com")
    print("  2. Menu (top right) > Senders, Domains > Senders > Add sender:")
    print("     add your mail id and click the link Brevo sends to it.")
    print("  3. Menu > SMTP & API > API keys > Generate a new API key. Copy it.\n")
    import config
    import mailer
    while True:
        sender = ask("Your mail id (the sender you added in Brevo)")
        key = input("Brevo API key (starts with xkeysib-): ").strip()
        if not sender or not key:
            continue
        print("  Sending a test mail to you ...")
        try:
            mailer.send_brevo(key, sender, sender, f"{config.BRAND}: test mail works",
                              "If you can read this, OTP mails will be delivered too.")
            print(f"  Sent. Check the inbox of {sender} (and Spam).")
            break
        except Exception as e:
            print(f"  It did not work: {e}")
            if not ask_yes("Type the details again?"):
                print("  Saved anyway - OTP mails will fail until this is corrected.")
                break
    return dict(NO_MAIL, MAIL_FROM=sender, BREVO_API_KEY=key)


def ask_mail():
    kind = ask_choice("How should the OTP mails be sent?", [
        "Gmail or Google Workspace (needs a 16-letter app password)",
        "Outlook / Office 365",
        "Another mail server",
        "Brevo - free, works when the office network blocks mail (recommended if 1 fails)",
        "Set up mail later (testing only - the code is shown in this window)",
    ])
    if kind == 5:
        return dict(NO_MAIL, DEV_MODE="1")
    if kind == 4:
        return ask_brevo()
    host = {1: "smtp.gmail.com", 2: "smtp.office365.com"}.get(kind) or ask("Mail server (SMTP host)")
    port = "587" if kind != 3 else ask("Mail server port", "587")
    if kind == 1:
        print("\n  Gmail needs an APP PASSWORD (16 letters), not your normal password:")
        print("  1. Sign in to the SAME mail id you will type below")
        print("  2. Open https://myaccount.google.com/apppasswords")
        print("     (2-Step Verification must be on)")
        print("  3. Create one named FaceFind and copy the 16 letters.\n")
    while True:
        user = ask("Mail id (e.g. you@gmail.com)")
        password = input("App password / mail password: ").replace(" ", "")
        if not user or not password:
            continue
        if kind == 1 and len(password) != 16:
            print(f"  That has {len(password)} characters. A Gmail app password has exactly 16 letters.")
            if ask_yes("Type it again?"):
                continue
        print("  Checking the mail login ...")
        problem, good_port = test_mail(host, int(port), user, password)
        if not problem:
            port = str(good_port)
            print("  Mail login works.")
            break
        print("  " + problem)
        if not ask_yes("Type the mail details again?"):
            print("  Saved anyway - OTP mails will fail until this is corrected.")
            break
    return dict(NO_MAIL, SMTP_HOST=host, SMTP_PORT=port, SMTP_USER=user,
                SMTP_PASSWORD=password, MAIL_FROM=user)


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
