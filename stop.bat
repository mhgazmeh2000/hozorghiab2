@echo off
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ids=@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq 5000 -or $_.LocalPort -eq 8081 } | Select-Object -ExpandProperty OwningProcess -Unique); if($ids.Count){foreach($id in $ids){taskkill /PID $id /F /T | Out-Null}; Write-Host 'Hozor application stopped.'}else{Write-Host 'Hozor application is not running.'}"

pause
