@echo off
rem Starts the Hazardous Zone Detector in the background - no window stays open -
rem and opens it in the browser when it is ready. Stop it with stop.bat or the
rem "Stop Hazardous Zone Detector" shortcut. (The Desktop shortcut does the same
rem as this file without even a brief flash of this window.)
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo Setup has not been run on this computer yet.
  echo Double-click install_windows.bat first, then try again.
  pause
  exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0scripts\launcher.py"
