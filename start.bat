@echo off
setlocal
cd /d "%~dp0"

if not exist logs mkdir logs
echo Starting Hozor application in background...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Start-Process -FilePath 'python.exe' -ArgumentList 'app.py' -WorkingDirectory '%~dp0' -RedirectStandardOutput '%~dp0logs\server.log' -RedirectStandardError '%~dp0logs\server.err.log' -WindowStyle Hidden -PassThru; Write-Host ('Started with PID ' + $p.Id)"
echo The application is running at http://127.0.0.1:5000
pause
