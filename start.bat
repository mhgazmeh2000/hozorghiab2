@echo off
setlocal
cd /d "%~dp0"

echo Starting Hozor application...
python app.py

if errorlevel 1 (
    echo.
    echo The application stopped with an error.
)
pause
