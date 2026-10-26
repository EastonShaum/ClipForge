@echo off
REM One-command start for the ClipForge laptop worker. Pulls jobs from the Pi over Tailscale.
REM Change PI_URL if your Pi's Tailscale name differs.
set PI_URL=http://raspberrypi:8420
cd /d "%~dp0\..\.."
echo ClipForge laptop worker -> %PI_URL%  (Ctrl+C to stop)
uv run clipforge remote-worker --pi-url %PI_URL% --kinds fetch,transcribe,render
pause
