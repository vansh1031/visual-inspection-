@echo off
rem Starts the Hazardous Zone Detector and opens it in the browser when it is ready.
rem This version keeps a console window open to show the server output, which
rem helps with troubleshooting. For everyday use, use the "Hazardous Zone Detector"
rem shortcut or start.bat, which run without a window.
title Hazardous Zone Detector
setlocal
cd /d "%~dp0.."

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Setup has not been run on this computer yet.
  echo.
  echo Double-click install_windows.bat in the project folder, wait until it
  echo says "Setup finished", then start the app again.
  echo.
  pause
  exit /b 1
)

echo Starting the Hazardous Zone Detector...
echo The browser opens by itself when the app is ready. Keep this window open.
echo.
set OPEN_BROWSER=1
"%PY%" hazard_server.py

echo.
echo The app has stopped. If you did not stop it yourself, the reason is shown above.
echo Run install_windows.bat again, or send a screenshot of this window for help.
pause
