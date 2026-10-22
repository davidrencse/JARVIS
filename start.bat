@echo off
rem JARVIS: start the voice listener (if it isn't already running), then open the
rem console as its own app window. Closing the window stops the console server; the
rem listener keeps running. Double-click it, or run it from any folder.
setlocal
cd /d "%~dp0"
title JARVIS

rem MiniConda has the voice packages; the PATH python doesn't.
set "PY=%USERPROFILE%\MiniConda3\python.exe"
set "PYW=%USERPROFILE%\MiniConda3\pythonw.exe"
if not exist "%PY%" set "PY=python"
if not exist "%PYW%" set "PYW=pythonw"

echo Starting JARVIS...
"%PY%" -m jarvis voice start >nul 2>&1
if errorlevel 1 echo   The voice listener didn't start; see .jarvis\logs\voice.log

rem Windowless: the app window is the UI. Console log: .jarvis\logs\console.log
start "" "%PYW%" -m jarvis app
