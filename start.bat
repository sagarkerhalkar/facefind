@echo off
cd /d "%~dp0"
if not exist .venv ( python -m venv .venv )
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
python download_models.py || pause
if not exist .env ( copy .env.example .env & echo. & echo Fill in the .env file, then run start.bat again. & notepad .env & exit /b )
python app.py
pause
