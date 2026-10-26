@echo off
REM ClipForge laptop worker: updates itself from the Pi, then pulls jobs over Tailscale.
REM cmd.exe reads a running .bat as it goes, so run from a temp copy; an update may then
REM safely replace this file.
if not "%~1"=="--inner" (
  copy /y "%~f0" "%TEMP%\clipforge-worker.bat" >nul
  "%TEMP%\clipforge-worker.bat" --inner "%~dp0"
)
REM Change PI_URL if your Pi's Tailscale name differs (e.g. http://100.x.y.z:8420).
set PI_URL=http://raspberrypi:8420
cd /d "%~2..\.."
REM winget's ffmpeg shim folder; a window opened before the install doesn't have it on PATH.
set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"

:loop
echo ClipForge laptop worker, Pi at %PI_URL%. Close this window to stop.
uv run clipforge self-update --pi-url %PI_URL%
uv run clipforge remote-worker --pi-url %PI_URL% --kinds fetch,transcribe,render
REM Exit code 3 = the Pi has newer code: loop back, update, restart.
if %ERRORLEVEL%==3 goto loop
pause
