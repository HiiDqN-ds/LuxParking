@echo off
rem One-click local start for Windows: double-click this file.
cd /d "%~dp0"
where python >nul 2>nul || (echo Install Python 3.11+ from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b 1)
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt || (pause & exit /b 1)
for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do set "%%a=%%~b"
for /f %%i in ('python lan_ip.py') do set "LAN_IP=%%i"
if not defined QR_BASE_URL set "QR_BASE_URL=http://%LAN_IP%:5000"
flask --app wsgi db upgrade || (pause & exit /b 1)
flask --app wsgi seed || (pause & exit /b 1)
echo.
echo On your phone (same Wi-Fi): %QR_BASE_URL%
echo Website:  http://127.0.0.1:5000/
if not defined SEED_LOT_SLUG set "SEED_LOT_SLUG=ratingen"
echo Booking:  http://127.0.0.1:5000/reserve/luxury-parking/%SEED_LOT_SLUG%
echo Admin:    http://127.0.0.1:5000/admin/login  (owner: %SEED_OWNER_EMAIL%, platform admin: %SEED_ADMIN_EMAIL%)
echo.
start "" http://127.0.0.1:5000/
flask --app wsgi run --debug --host 0.0.0.0 --port 5000
pause
