# ClipForge laptop worker setup (Windows 10, native, no WSL/Docker).
# Run in PowerShell from the repo folder:  powershell -ExecutionPolicy Bypass -File scripts\laptop\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\..")

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv (Python manager)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "Installing ffmpeg via winget..."
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    Write-Host "Close and reopen PowerShell so ffmpeg is on PATH, then re-run this script."
    exit 0
}
uv sync --frozen --no-dev
if (-not (Test-Path .env)) {
    @"
CF_HOST_NAME=laptop
CF_DATA_DIR=$((Get-Location).Path)\data
CF_MEDIA_CAP_GB=200
CF_MIN_FREE_GB=20
CF_WORKER_TOKEN=PASTE_SAME_TOKEN_AS_PI
CF_TRANSCRIBE_PROVIDER=groq
CF_GROQ_API_KEY=PASTE_GROQ_KEY
"@ | Set-Content -Encoding utf8 .env
    Write-Host "Created .env - open it and paste the worker token and Groq key."
}
Write-Host "Setup done. Start the worker with scripts\laptop\start-worker.bat"
