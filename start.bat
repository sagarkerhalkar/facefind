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
python -m pip install --no-cache-dir --retries 5 --timeout 60 -r requirements-formats.txt
if errorlevel 1 echo (HEIC / RAW support could not be installed - the app still works for other photos.)
python download_models.py
if errorlevel 1 (
  pause
  exit /b 1
)
if not exist .env (
  python setup.py
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
:run
python app.py
if errorlevel 75 goto run
pause
