@echo off
setlocal
rem Windows PowerShell must build its own module path when called from PowerShell 7.
set "PSModulePath="
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0univpn.ps1" %*
exit /b %errorlevel%
