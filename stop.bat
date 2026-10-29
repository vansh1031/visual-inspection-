@echo off
rem Stops the detector that start.bat or the Desktop shortcut started.
if not exist "%~dp0.venv\Scripts\pythonw.exe" exit /b 0
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0scripts\launcher.py" --stop
