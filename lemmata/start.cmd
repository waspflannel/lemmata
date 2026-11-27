@echo off
rem Start the lemmata math shell. Flags: -Research, -Check. See start.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
