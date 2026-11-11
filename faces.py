"""Face detection + face signatures (OpenCV YuNet + SFace).

To use a different model (e.g. InsightFace on a GPU), write a class with the
same `embed(bgr_image) -> list of unit vectors` method and return it from
get_engine(). Re-index after changing the model.
"""
import threading

import cv2
import numpy as np

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

    def embed(self, bgr, max_side=1600, min_face=36):
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
                v = self.rec.feature(self.rec.alignCrop(bgr, f)).flatten().astype(np.float32)
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


def decode(data: bytes):
    """bytes -> BGR image (EXIF rotation applied) or None."""
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
