@echo off
rem Chain headless lemmata rounds on one challenge. See rounds.ps1 for flags.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0rounds.ps1" %*
