import json
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage

import config


def send_brevo(api_key, sender, to, subject, text):
    """Sends through Brevo's web API (normal HTTPS, port 443) - for networks
    that block the mail ports."""
    body = json.dumps({"sender": {"email": sender, "name": config.BRAND},
                       "to": [{"email": to}], "subject": subject,
                       "textContent": text}).encode()
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email", data=body,
        headers={"api-key": api_key, "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Brevo refused the mail ({e.code}): "
                           + e.read().decode("utf-8", "replace")[:200])


def send_smtp(to, subject, text):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, config.MAIL_FROM, to
    msg.set_content(text)
    ctx = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        s = smtplib.SMTP_SSL(config.SMTP_HOST, 465, context=ctx, timeout=20)
    else:
        s = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
        s.starttls(context=ctx)
    with s:
        s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)


def send(to, subject, text):
    if config.BREVO_API_KEY:
        send_brevo(config.BREVO_API_KEY, config.MAIL_FROM, to, subject, text)
    elif config.SMTP_HOST:
        send_smtp(to, subject, text)
    else:
        raise RuntimeError("Mail is not set up. Run mail.bat")


def send_otp(to, code, event_name):
    """Sends the one-time code from YOUR mail id."""
    if not config.BREVO_API_KEY and not config.SMTP_HOST and config.DEV_MODE:
        print(f"\n*** DEV MODE - OTP for {to}: {code} ***\n", flush=True)
        return
    minutes = config.OTP_TTL // 60
    send(to, f"{code} is your {config.BRAND} verification code",
         f"Your verification code for \"{event_name}\" is: {code}\n\n"
         f"It is valid for {minutes} minutes. Do not share it with anyone.\n"
         f"If you did not ask for this code, ignore this email.\n\n- {config.BRAND}")
