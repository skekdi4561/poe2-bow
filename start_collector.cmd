@echo off
rem Double-click launcher. First run asks for POESESSID once and stores it encrypted (DPAPI). Reset: start_collector.cmd -Reset
powershell -NoProfile -NoExit -ExecutionPolicy Bypass -File "%~dp0start_collector.ps1" %*
