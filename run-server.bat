@echo off
rem Used by the start-up task: no installing, no questions, and it never stops.
rem If the app ever exits (crash, damaged database repaired, port busy) it starts again.
cd /d "%~dp0"
if not exist data mkdir data
call .venv\Scripts\activate.bat
set NO_BROWSER=1
:run
echo %date% %time% starting >> data\service.log
python app.py >> data\service.log 2>&1
ping -n 6 127.0.0.1 >nul
goto run
