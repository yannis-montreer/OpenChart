@echo off
rem Installs or updates the OpenChart Windows service. Windows asks for administrator rights.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-service.ps1" %*
