@echo off
REM Double-click (or right-click -> Run as administrator) to deploy RustDesk on this PC.
REM Runs Deploy-RustDesk.ps1 from the same folder without changing the PC's
REM PowerShell execution policy permanently.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Deploy-RustDesk.ps1" %*
echo.
pause
