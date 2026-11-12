"""Run once:  python download_models.py   (downloads the two face models, ~40 MB)"""
import urllib.request
from pathlib import Path

ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models/"
FILES = {
    "face_detection_yunet_2023mar.onnx": "face_detection_yunet/",
    "face_recognition_sface_2021dec.onnx": "face_recognition_sface/",
}
out = Path(__file__).resolve().parent / "models"
out.mkdir(exist_ok=True)
for name, sub in FILES.items():
    dest = out / name
    if dest.exists() and dest.stat().st_size > 100_000:
        print("already there:", name)
        continue
    print("downloading", name, "...")
    for attempt in range(1, 6):
        try:
            urllib.request.urlretrieve(ZOO + sub + name, dest)
            if dest.stat().st_size > 100_000:
                break
        except Exception as e:
            print(f"  attempt {attempt} failed: {e}")
        dest.unlink(missing_ok=True)
    else:
        raise SystemExit(f"Download of {name} failed - check the internet and run again.")
print("Models ready.")
