@echo off
setlocal

echo === Shazam2Spotify Installer ===
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: python not found. Install Python 3.9+ from python.org and try again.
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"') do set PYVER=%%v
python -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"
if errorlevel 1 (
    echo ERROR: Python 3.9+ required ^(found %PYVER%^).
    pause
    exit /b 1
)
echo Python %PYVER% found.

echo Creating virtual environment...
python -m venv .venv
if errorlevel 1 (
    echo ERROR: Failed to create virtual environment.
    pause
    exit /b 1
)

echo Installing dependencies...
.venv\Scripts\pip install --upgrade pip -q
.venv\Scripts\pip install -r requirements.txt -q
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

(
echo @echo off
echo cd /d "%%~dp0"
echo call .venv\Scripts\activate
echo python web_app.py %%*
) > run.bat

echo.
echo === Done! ===
echo.
echo Run the app with:  run.bat
echo Debug mode:        run.bat --debug
echo.
echo Then open http://127.0.0.1:5000 in your browser.
pause
