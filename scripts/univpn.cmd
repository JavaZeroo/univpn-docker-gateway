@echo off
setlocal
rem Windows PowerShell must build its own module path when called from PowerShell 7.
set "PSModulePath="
set "UNIVPN_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if defined PROCESSOR_ARCHITEW6432 set "UNIVPN_POWERSHELL=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
"%UNIVPN_POWERSHELL%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0univpn.ps1" %*
exit /b %errorlevel%
