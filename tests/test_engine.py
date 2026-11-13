"""Checks that the real face models load and run. Needs the model files
(python download_models.py). Run:  python tests/test_engine.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cv2  # noqa: E402
import numpy as np  # noqa: E402

import faces  # noqa: E402

engine = faces.get_engine()
blank = np.full((1200, 1600, 3), 120, np.uint8)
assert engine.embed(blank) == [], "a blank picture has no face"
ok, jpg = cv2.imencode(".jpg", np.full((4000, 6000, 3), 120, np.uint8))
small = faces.decode_fast(jpg.tobytes(), want=1600)
assert small.shape[:2] == (2000, 3000), small.shape     # big photos open at reduced size
assert faces.decode_fast(b"not a picture") is None
print("ok  face models load, blank picture gives no face, fast decode works")
