@echo off
setlocal
cd /d "%~dp0"
set "PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" app.py
if errorlevel 1 (
  echo.
  echo Super exited with an error. See the messages above.
  pause
)
