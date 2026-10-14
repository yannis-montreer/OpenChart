OpenChart server for Windows
============================

1. Run install-service.cmd (double-click; Windows asks for administrator rights).
   It installs the "OpenChart" service: it starts with Windows and restarts by itself,
   keeps its data in C:\ProgramData\OpenChart\data, and opens port 8000 to Tailscale
   addresses only (100.64.0.0/10). It also warns if the PC is set to sleep.
2. Open http://localhost:8000 on this PC and follow the setup page: a password, your OANDA
   practice account ID and token, the instrument (XAU_USD). Add Telegram in Settings.
3. From your other devices (with Tailscale): http://<this PC's name>:8000, or the
   OpenChart Viewer app.

Options: install-service.ps1 -Port 8080, -DataDir D:\OpenChart, -WhatIf (see its help:
Get-Help .\install-service.ps1 -Full). Remove: uninstall-service.cmd (keeps the data).
Back up: openchart\openchart.exe backup --data-dir C:\ProgramData\OpenChart\data

Full guide: https://github.com/yannis-montreer/OpenChart#quickstart
Licences: LICENSE.txt, NOTICE.txt, THIRD-PARTY-LICENSES.txt
