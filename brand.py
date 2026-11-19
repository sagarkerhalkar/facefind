"""What the studio owner changes from the admin pages: studio name, colour,
logo, sign-in background, the logo put on downloaded photos, promotions,
and staff passwords."""
import hashlib
import functools
import hmac
import io
import re
import secrets

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

import config
import db

BRAND_DIR = config.DATA_DIR / "brand"
BRAND_DIR.mkdir(parents=True, exist_ok=True)
LOGO, BACKGROUND = BRAND_DIR / "logo.png", BRAND_DIR / "background.jpg"
BACKGROUND_PNG = BRAND_DIR / "background.png"       # used when the picture has a see-through background
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
        im = clear_background(im.convert("RGBA"))        # a logo must be see-through
        im.save(dest, "PNG", optimize=True)
    else:
        im = im.convert("RGB")
        im.save(dest, "JPEG", quality=90)


def _corners(im):
    w, h = im.size
    return [im.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]


def _alike(colours, tol):
    return all(max(abs(a - b) for a, b in zip(c[:3], colours[0][:3])) <= tol for c in colours)


def clear_background(im):
    """A logo saved on a white (or any one-colour) background gets that
    background made transparent, so it does not show as a box on photos.
    Pictures that already have transparency are left alone."""
    im = im.convert("RGBA")
    corners = _corners(im)
    if any(c[3] < 250 for c in corners) or not _alike(corners, 18):
        return im
    if im.getchannel("A").getextrema()[0] < 250:
        return im
    marker = (1, 2, 3)
    flooded = im.convert("RGB")
    for p in ((0, 0), (im.width - 1, 0), (0, im.height - 1), (im.width - 1, im.height - 1)):
        if flooded.getpixel(p) != marker:
            ImageDraw.floodfill(flooded, p, marker, thresh=40)
    gone = ImageChops.difference(flooded, Image.new("RGB", im.size, marker)).convert("L").point(
        lambda v: 0 if v == 0 else 255)                   # 0 where the background was reached
    if gone.getextrema()[1] == 0 or sum(gone.histogram()[255:]) < 0.02 * im.width * im.height:
        return im                                         # nothing but background: leave it as it is
    gone = gone.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.6))   # no light fringe
    im.putalpha(gone)
    return im


def trim_border(im, tol=24):
    """Cuts away an even-coloured margin (for example white) around a picture."""
    im = im.convert("RGB")
    corners = _corners(im)
    if not _alike(corners, 18):
        return im
    diff = ImageChops.difference(im, Image.new("RGB", im.size, corners[0])).convert("L")
    box = diff.point(lambda v: 255 if v > tol else 0).getbbox()
    if not box:
        return im
    pad = max(8, int(max(im.size) * 0.02))
    box = (max(0, box[0] - pad), max(0, box[1] - pad),
           min(im.width, box[2] + pad), min(im.height, box[3] + pad))
    return im.crop(box)


@functools.lru_cache(maxsize=2)
def _logo(stamp):
    return clear_background(Image.open(LOGO).convert("RGBA"))


def logo_image():
    """The logo as a transparent picture (also for logos saved before this existed)."""
    return _logo(LOGO.stat().st_mtime_ns).copy()


def logo_png():
    out = io.BytesIO()
    logo_image().save(out, "PNG")
    return out.getvalue()


def save_background(upload):
    """The sign-in picture. A cut-out with a see-through background stays
    see-through (PNG); any other picture loses its empty margin (JPG)."""
    data = upload.read(12 * 1024 * 1024 + 1)
    if not data or len(data) > 12 * 1024 * 1024:
        raise ValueError("Choose a picture smaller than 12 MB.")
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        im.load()
    except Exception:
        raise ValueError("That file is not a picture. Use JPG, PNG or WEBP.")
    im.thumbnail((1800, 1800))
    rgba = im.convert("RGBA")
    clear = rgba.getchannel("A").getextrema()[0] < 250
    remove_background()
    if clear:
        box = rgba.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
        (rgba.crop(box) if box else rgba).save(BACKGROUND_PNG, "PNG", optimize=True)
    else:
        trim_border(im.convert("RGB")).save(BACKGROUND, "JPEG", quality=90)


def remove_background():
    for f in (BACKGROUND, BACKGROUND_PNG):
        f.unlink(missing_ok=True)


def background_file():
    """The stored sign-in picture and its type, or (None, None)."""
    for f, mime in ((BACKGROUND_PNG, "image/png"), (BACKGROUND, "image/jpeg")):
        if f.exists():
            return f, mime
    return None, None


def has_transparent_background():
    return BACKGROUND_PNG.exists()


@functools.lru_cache(maxsize=2)
def _ratio(stamp):
    f, _ = background_file()
    with Image.open(f) as im:
        return round(im.height / im.width, 3)


def background_ratio():
    """Height divided by width of the sign-in picture (phones leave room for it)."""
    f, _ = background_file()
    return _ratio(f.stat().st_mtime_ns) if f else 0.7


def version():
    """Changes when a brand picture changes, so browsers fetch the new one."""
    return str(int(max([f.stat().st_mtime for f in (LOGO, BACKGROUND, BACKGROUND_PNG) if f.exists()] or [0])))


def add_logo(im):
    """Puts the studio logo, small, in the top right corner of a picture."""
    logo = logo_image()
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
