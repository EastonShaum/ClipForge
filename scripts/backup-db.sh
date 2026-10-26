#!/usr/bin/env bash
# Consistent online backup of the SQLite DB (safe while services run). Keeps 14 copies.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/backups
out="data/backups/clipforge-$(date +%Y%m%d-%H%M%S).db"
sqlite3 data/clipforge.db ".backup '$out'"
ls -1t data/backups/clipforge-*.db | tail -n +15 | xargs -r rm --
echo "backup: $out"
