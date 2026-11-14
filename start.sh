#!/bin/sh
cd "$(dirname "$0")" || exit 1
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
pip install -q -r requirements-formats.txt || echo "(HEIC / RAW support could not be installed - the app still works for other photos.)"
python download_models.py || exit 1
[ -f .env ] || python setup.py || exit 1
while true; do
  python app.py
  [ $? -eq 3 ] || break
  echo 'The database was damaged - repairing it and starting again ...'; sleep 2
done
