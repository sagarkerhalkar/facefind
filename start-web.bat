@echo off
rem Puts FaceFind on the internet through a Cloudflare tunnel:
rem no router changes, no fixed IP, HTTPS included. See README, "On the web".
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
rem The tunnel brings HTTPS; the app itself listens only on this computer.
set HTTPS=0
set TRUST_PROXY=1
set HOST=127.0.0.1
set PORT=8787
set NO_BROWSER=1
start "FaceFind app" cmd /k start.bat
echo Waiting for the app to start ...
timeout /t 25 /nobreak >nul
echo.
echo  ------------------------------------------------------------------
echo   Your web address is the line below that ends in trycloudflare.com
echo   Guests:  that address          Admin:  that address + /admin
echo   Keep this window open. The address changes each time you start it;
echo   for a fixed address on your own domain see README, "On the web".
echo  ------------------------------------------------------------------
echo.
cloudflared.exe tunnel --url http://127.0.0.1:8787
pause
