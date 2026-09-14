@echo off
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=(Resolve-Path -LiteralPath '%~dp0').Path.TrimEnd('\'); $found=$false; Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\" | Where-Object { $_.CommandLine -like \"*$root*app.py*\" } | ForEach-Object { $found=$true; Stop-Process -Id $_.ProcessId -Force }; if ($found) { Write-Host 'Hozor application stopped.' } else { Write-Host 'Hozor application is not running.' }"

pause
