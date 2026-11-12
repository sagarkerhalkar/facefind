"""Downloads the two face models (~40 MB) and checks that they really load.
Runs at every start; a damaged file is downloaded again automatically."""
import subprocess
import sys
import urllib.request
from pathlib import Path

ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models/"
FILES = {
    "face_detection_yunet_2023mar.onnx": "face_detection_yunet/",
    "face_recognition_sface_2021dec.onnx": "face_recognition_sface/",
}
out = Path(__file__).resolve().parent / "models"
out.mkdir(exist_ok=True)
CHECK = ("import cv2,sys;"
         "cv2.FaceDetectorYN.create(sys.argv[1],'',(320,320));"
         "cv2.FaceRecognizerSF.create(sys.argv[2],'')")


def models_load():
    paths = [str(out / n) for n in FILES]
    r = subprocess.run([sys.executable, "-c", CHECK, *paths], capture_output=True, text=True)
    return r.returncode == 0


def download(name, sub):
    dest, part = out / name, out / (name + ".part")
    for attempt in range(1, 6):
        try:
            print(f"downloading {name} (attempt {attempt}) ...", flush=True)
            urllib.request.urlretrieve(ZOO + sub + name, part)
            if part.stat().st_size > 100_000:
                part.replace(dest)      # only a complete file gets the real name
                return
        except Exception as e:
            print("  failed:", e)
        part.unlink(missing_ok=True)
    raise SystemExit(f"Could not download {name}. Check the internet and run again.")


def main():
    missing = [n for n in FILES if not (out / n).exists()]
    for n in missing:
        download(n, FILES[n])
    if models_load():
        print("Face models ready.")
        return
    print("Face model files are damaged - downloading them again ...")
    for n, sub in FILES.items():
        (out / n).unlink(missing_ok=True)
        download(n, sub)
    if models_load():
        print("Face models repaired and ready.")
        return
    print("Models still do not load with this OpenCV version - installing OpenCV 4 ...")
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir",
                    "opencv-python>=4.9,<5"])
    if models_load():
        print("Face models ready (OpenCV 4).")
        return
    raise SystemExit("The face models could not be loaded. Please send this "
                     "window's text to the developer.")


if __name__ == "__main__":
    main()
