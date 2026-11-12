"""Settings. Everything is read from the .env file next to this file."""
import os
import secrets
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _load_env():
    p = BASE / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def _b(name, default="0"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = Path(os.environ.get("DATA_DIR", BASE / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR = DATA_DIR / "previews"
CACHE_DIR.mkdir(exist_ok=True)
SELFIE_DIR = DATA_DIR / "pending_selfies"
SELFIE_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "facefind.db"
GOOGLE_KEY = DATA_DIR / "google-key.json"           # uploaded on the admin page
DRIVE_DIR = Path(os.environ.get("DRIVE_CACHE", DATA_DIR / "drive"))  # old versions copied Drive photos here
MODELS_DIR = Path(os.environ.get("MODELS_DIR", BASE / "models"))


def _secret():
    s = os.environ.get("SECRET_KEY")
    if s:
        return s
    f = DATA_DIR / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        try:
            f.chmod(0o600)
        except OSError:
            pass
    return f.read_text().strip()


SECRET_KEY = _secret()

BRAND = os.environ.get("BRAND", "FaceFind")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# DEV_MODE: OTP is printed in the server window when no mail is configured,
# and cookies work over plain http. NEVER switch on for real guests.
DEV_MODE = _b("DEV_MODE")
TRUST_PROXY = _b("TRUST_PROXY")

# --- network -----------------------------------------------------------
import netutil  # noqa: E402

HOST = os.environ.get("HOST", "0.0.0.0")        # 0.0.0.0 = reachable on the LAN
PORT = int(os.environ.get("PORT", "8443"))      # if busy, the next free port is used
HTTPS = _b("HTTPS", "1")                        # built-in HTTPS for the LAN
TLS_NAMES = os.environ.get("TLS_NAMES", "").split(",")   # extra names for the certificate
TLS_DIR = DATA_DIR / "tls"
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")  # e.g. https://photos.mydomain.com
ALLOWED_NETWORKS = netutil.parse_networks(os.environ.get("ALLOWED_NETWORKS", ""))
ADMIN_NETWORKS = netutil.parse_networks(os.environ.get("ADMIN_NETWORKS", ""))
COOKIE_SECURE = HTTPS or not DEV_MODE

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER)
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")   # mail over HTTPS (when mail ports are blocked)

OTP_TTL = int(os.environ.get("OTP_TTL_SECONDS", "300"))
OTP_MAX_ATTEMPTS = 5
OTP_PER_EMAIL = 3          # sends per 15 minutes
OTP_PER_IP = 10            # sends per 15 minutes
SESSION_TTL = int(os.environ.get("SESSION_HOURS", "2")) * 3600
SELFIE_MAX_TRIES = 30      # per verified session; after that the email is verified again
DOWNLOAD_LINK_TTL = 120    # seconds
# 0.363 is the value the face model's authors publish for "same person".
# (0.40 was this app's first default and proved too strict for selfies.)
_thr = os.environ.get("MATCH_THRESHOLD", "").strip()
MATCH_THRESHOLD = 0.363 if _thr in ("", "0.40") else float(_thr)
MAX_RESULTS = 500
PREVIEW_SIDE = 900
# Photos scanned at the same time. More = faster, but the PC gets busier.
SCAN_WORKERS = int(os.environ.get("SCAN_WORKERS", "0")) or max(1, min(6, (os.cpu_count() or 2) - 1))
# Google Drive photos are read straight from Drive into memory (never saved).
DRIVE_WORKERS = int(os.environ.get("DRIVE_WORKERS", "8"))   # Drive photos read at the same time
# Size (longest side, pixels) at which photos are searched for faces. 1600 finds
# small faces in group photos; 1280 is about a third faster but can miss them.
SCAN_SIDE = int(os.environ.get("SCAN_SIDE", "1600"))
DRIVE_SCAN_SIDE = SCAN_SIDE  # size asked from Drive for scanning
# One general guest link: the guest signs in once with email, picks the dates,
# and all open albums with a date in that range are searched.
PORTAL = _b("PORTAL", "1")
MAX_PHOTO_BYTES = 120 * 1024 * 1024
OTHER_EXT = {".heic", ".heif", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".rw2",
             ".orf", ".tif", ".tiff", ".gif", ".psd"}   # seen but not readable
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
