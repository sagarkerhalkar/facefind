@echo off
rem Puts FaceFind on the internet through a Cloudflare tunnel:
rem no router changes, no fixed IP. The app keeps its one port, so the
rem office-network address and the web address work at the same time.
cd /d "%~dp0"
if not exist cloudflared.exe (
  echo Downloading the Cloudflare tunnel program ...
  curl -L --fail -o cloudflared.exe https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
  if errorlevel 1 (
    echo Could not download it. Check the internet connection and run this file again.
    pause
    exit /b 1
  )
)
rem Lets the app see the real address of web visitors.
set TRUST_PROXY=1
set NO_BROWSER=1
start "FaceFind app" cmd /k start.bat
echo Waiting for the app to start ...
timeout /t 30 /nobreak >nul
if not exist data\port.txt (
  echo The app has not started yet. Look at the other window, then run this file again.
  pause
  exit /b 1
)
set /p FFPORT=<data\port.txt
echo.
echo  ------------------------------------------------------------------
echo   The app runs on port %FFPORT% (always the same).
echo   Your web address is the line below that ends in trycloudflare.com
echo   Guests:  that address          Admin:  that address + /admin
echo   Keep this window open. For a fixed address on your own domain
echo   see README, "On the web".
echo  ------------------------------------------------------------------
echo.
cloudflared.exe tunnel --url https://127.0.0.1:%FFPORT% --no-tls-verify
pause
