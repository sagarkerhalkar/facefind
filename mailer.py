import smtplib
import ssl
from email.message import EmailMessage

import config


def send_otp(to, code, event_name):
    """Sends the one-time code from YOUR mail id (set in .env)."""
    if not config.SMTP_HOST:
        if config.DEV_MODE:
            print(f"\n*** DEV MODE - OTP for {to}: {code} ***\n", flush=True)
            return
        raise RuntimeError("Mail is not configured (SMTP_HOST missing in .env)")
    minutes = config.OTP_TTL // 60
    msg = EmailMessage()
    msg["Subject"] = f"{code} is your {config.BRAND} verification code"
    msg["From"] = config.MAIL_FROM
    msg["To"] = to
    msg.set_content(
        f"Your verification code for \"{event_name}\" is: {code}\n\n"
        f"It is valid for {minutes} minutes. Do not share it with anyone.\n"
        f"If you did not ask for this code, ignore this email.\n\n- {config.BRAND}")
    ctx = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(config.SMTP_HOST, 465, context=ctx, timeout=20) as s:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
            s.starttls(context=ctx)
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)
