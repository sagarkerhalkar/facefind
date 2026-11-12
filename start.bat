@echo off
cd /d "%~dp0"
if not exist .venv ( python -m venv .venv )
call .venv\Scripts\activate.bat
python -m pip install --no-cache-dir --retries 10 --timeout 60 -r requirements.txt
if errorlevel 1 (
  echo.
  echo Install did not finish - usually the internet connection dropped. Run start.bat again.
  pause
  exit /b 1
)
python download_models.py
if errorlevel 1 (
  pause
  exit /b 1
)
if not exist .env ( copy .env.example .env & echo. & echo Fill in the .env file, then run start.bat again. & notepad .env & exit /b )
python app.py
pause
