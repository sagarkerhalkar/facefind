"""What the studio owner changes from the admin pages: studio name, colour,
logo, sign-in background, the logo put on downloaded photos, promotions,
and staff passwords."""
import hashlib
import hmac
import io
import re
import secrets

from PIL import Image, ImageOps

import config
import db

BRAND_DIR = config.DATA_DIR / "brand"
BRAND_DIR.mkdir(parents=True, exist_ok=True)
LOGO, BACKGROUND = BRAND_DIR / "logo.png", BRAND_DIR / "background.jpg"
DEFAULTS = {
    "brand": config.BRAND,
    "tagline": "Every photo you are in, found with one selfie.",
    "accent": "#E8A317",          # marigold
    "stamp": "1",                 # 1 = put the logo on downloaded photos (default ON)
    "scan_side": str(config.SCAN_SIDE),
    "look_first": "1",            # 1 = guests may see previews before signing in
}
COLOUR_RE = re.compile(r"#[0-9a-fA-F]{6}")
STAMPABLE = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".bmp", ".tif", ".tiff"}
try:                                    # iPhone photos can be stamped too
    import pillow_heif
    pillow_heif.register_heif_opener()
    STAMPABLE |= {".heic", ".heif"}
except Exception:
    pass


def get(c, key):
    return db.kv_get(c, "set:" + key) or DEFAULTS.get(key, "")


def put(c, key, value):
    db.kv_set(c, "set:" + key, value)


def load():
    """Applies saved settings when the app starts."""
    c = db.connect()
    try:
        config.BRAND = get(c, "brand")
        config.SCAN_SIDE = int(get(c, "scan_side"))
        config.DRIVE_SCAN_SIDE = max(1600, config.SCAN_SIDE)
        config.PREVIEW_TAG = version()
        v = db.kv_get(c, "match_threshold")
        if v:
            config.MATCH_THRESHOLD = float(v)
    finally:
        c.close()


def promo_image(pid):
    return BRAND_DIR / f"promo_{int(pid)}.jpg"


def save_image(upload, dest, max_side):
    """Stores an uploaded picture after re-drawing it (so only a real picture
    can be stored). PNG keeps transparency (logos); JPG for everything else."""
    data = upload.read(12 * 1024 * 1024 + 1)
    if not data or len(data) > 12 * 1024 * 1024:
        raise ValueError("Choose a picture smaller than 12 MB.")
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        im.load()
    except Exception:
        raise ValueError("That file is not a picture. Use JPG, PNG or WEBP.")
    im.thumbnail((max_side, max_side))
    if dest.suffix == ".png":
        im.convert("RGBA").save(dest, "PNG", optimize=True)
    else:
        im.convert("RGB").save(dest, "JPEG", quality=88)


def version():
    """Changes when a brand picture changes, so browsers fetch the new one."""
    return str(int(max([f.stat().st_mtime for f in (LOGO, BACKGROUND) if f.exists()] or [0])))


def add_logo(im):
    """Puts the studio logo, small, in the top right corner of a picture."""
    logo = Image.open(LOGO).convert("RGBA")
    width = max(48, int(max(im.size) * 0.09))
    logo = logo.resize((width, max(1, round(logo.height * width / logo.width))), Image.LANCZOS)
    margin = max(8, int(max(im.size) * 0.018))
    im.paste(logo, (im.width - logo.width - margin, margin), logo)
    return im


def stamp(data, ext):
    """A downloaded photo with the studio logo, small, in the top right
    corner. Returns JPEG bytes, or None when this file type cannot be
    stamped (RAW, HEIC) or no logo is set - the original is then sent."""
    if ext.lower() not in STAMPABLE or not LOGO.exists():
        return None
    try:
        im = Image.open(io.BytesIO(data))
        icc = im.info.get("icc_profile")
        im = ImageOps.exif_transpose(im)
        exif = im.getexif()
        im = im.convert("RGB")
        add_logo(im)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=95, subsampling=0, exif=exif.tobytes(),
                **({"icc_profile": icc} if icc else {}))
        return out.getvalue()
    except Exception:
        return None


# ------------------------------------------------------------ staff passwords
def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
    return salt, digest.hex()


def check_password(password, salt, digest):
    try:
        return hmac.compare_digest(hash_password(password, salt)[1], digest)
    except ValueError:
        return False
