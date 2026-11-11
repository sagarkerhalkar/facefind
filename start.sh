#!/bin/sh
cd "$(dirname "$0")" || exit 1
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
python download_models.py || exit 1
if [ ! -f .env ]; then cp .env.example .env; echo "Fill in the .env file, then run ./start.sh again."; exit 0; fi
python app.py
