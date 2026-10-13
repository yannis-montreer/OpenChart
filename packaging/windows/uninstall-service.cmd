@echo off
rem Removes the OpenChart Windows service and its firewall rule (keeps the data).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall-service.ps1" %*
