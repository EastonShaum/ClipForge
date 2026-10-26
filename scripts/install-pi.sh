#!/usr/bin/env bash
# Install/refresh ClipForge as systemd *user* services on the Pi. Safe to re-run.
# Requires lingering (loginctl enable-linger $USER) so services start at boot without login.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
uv sync --frozen --no-dev
[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example — fill it in."; }
.venv/bin/clipforge init
mkdir -p ~/.config/systemd/user
cp deploy/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now clipforge-web clipforge-worker-light clipforge-worker-render clipforge-scheduler
systemctl --user --no-pager status 'clipforge-*' | grep -E "●|Active:"
