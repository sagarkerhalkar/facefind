#!/bin/sh
cd "$(dirname "$0")" || exit 1
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
python download_models.py || exit 1
[ -f .env ] || python setup.py || exit 1
python app.py
