"""Face detection + face signatures (OpenCV YuNet + SFace).

To use a different model (e.g. InsightFace on a GPU), write a class with the
same `embed(bgr_image) -> list of unit vectors` method and return it from
get_engine(). Re-index after changing the model.
"""
import io
import threading

import cv2
import numpy as np
from PIL import Image

import config

DET = "face_detection_yunet_2023mar.onnx"
REC = "face_recognition_sface_2021dec.onnx"


class OpenCVEngine:
    def __init__(self):
        det, rec = config.MODELS_DIR / DET, config.MODELS_DIR / REC
        if not det.exists() or not rec.exists():
            raise RuntimeError(
                "Face models not found. Run:  python download_models.py")
        self.det = cv2.FaceDetectorYN.create(str(det), "", (320, 320), 0.8, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(str(rec), "")
        self.lock = threading.Lock()

    def embed(self, bgr, max_side=None, min_face=36):
        max_side = max_side or config.SCAN_SIDE
        h, w = bgr.shape[:2]
        s = min(1.0, max_side / max(h, w))
        if s < 1.0:
            bgr = cv2.resize(bgr, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
            h, w = bgr.shape[:2]
        out = []
        with self.lock:
            self.det.setInputSize((w, h))
            _, found = self.det.detect(bgr)
            if found is None:
                return out
            for f in found:
                if f[2] < min_face or f[3] < min_face:
                    continue
                # The face is read twice, as it is and mirrored, and the two
                # signatures are averaged: steadier than a single reading.
                crop = self.rec.alignCrop(bgr, f)
                v = self.rec.feature(crop).flatten().astype(np.float32)
                v = v + self.rec.feature(cv2.flip(crop, 1)).flatten().astype(np.float32)
                n = np.linalg.norm(v)
                if n > 0:
                    out.append(v / n)
        return out


_engine = None
_lock = threading.Lock()


def get_engine():
    global _engine
    with _lock:
        if _engine is None:
            _engine = OpenCVEngine()
        return _engine


_local = threading.local()


def thread_engine():
    """One engine per scanning thread, so several photos are scanned at once."""
    if not hasattr(_local, "engine"):
        _local.engine = make_engine()
    return _local.engine


def make_engine():
    return OpenCVEngine()


def decode_fast(data: bytes, want=None):
    """For scanning: a big camera photo is opened directly at 1/2, 1/4 or 1/8
    size (whichever still leaves `want` pixels), which is several times
    faster than opening it in full and shrinking it afterwards."""
    want = want or config.SCAN_SIDE
    arr = np.frombuffer(data, np.uint8)
    flag = cv2.IMREAD_COLOR
    try:
        big = max(Image.open(io.BytesIO(data)).size)      # reads the header only
        if big >= want * 8:
            flag = cv2.IMREAD_REDUCED_COLOR_8
        elif big >= want * 4:
            flag = cv2.IMREAD_REDUCED_COLOR_4
        elif big >= want * 2:
            flag = cv2.IMREAD_REDUCED_COLOR_2
    except Exception:
        pass
    img = cv2.imdecode(arr, flag)
    if img is None and flag != cv2.IMREAD_COLOR:
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img


def _embedded_jpeg(data):
    """Every camera RAW file carries a ready JPEG picture inside it. This
    finds the largest one - no extra software needed."""
    best, pos, tried = None, 0, 0
    while tried < 12:
        pos = data.find(b"\xff\xd8\xff", pos)
        if pos < 0:
            break
        tried += 1
        img = cv2.imdecode(np.frombuffer(data, np.uint8, offset=pos), cv2.IMREAD_COLOR)
        if img is not None and (best is None or img.size > best.size):
            best = img
        pos += 3
    return best


def _raw(data):
    try:
        import rawpy
    except ImportError:
        return _embedded_jpeg(data)
    try:
        with rawpy.imread(io.BytesIO(data)) as raw:
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    img = decode_fast(bytes(thumb.data))
                    if img is not None and max(img.shape[:2]) >= 1000:
                        return img
            except Exception:
                pass
            rgb = raw.postprocess(half_size=True, use_camera_wb=True)
            return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    except Exception:
        return _embedded_jpeg(data)


def _heic(data):
    try:
        import pillow_heif
    except ImportError:
        raise RuntimeError("HEIC photos need the 'pillow-heif' package - run start.bat again")
    pillow_heif.register_heif_opener()
    from PIL import ImageOps
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)


def load(data: bytes, ext=""):
    """Any supported photo file -> picture for scanning (or None).
    JPG, PNG, WEBP, BMP, TIFF directly; HEIC (iPhone); camera RAW files."""
    ext = ext.lower()
    if ext in config.HEIC_EXT:
        return _heic(data)
    if ext in config.RAW_EXT:
        return _raw(data)
    return decode_fast(data)


def decode(data: bytes):
    """bytes -> BGR image (EXIF rotation applied) or None."""
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
