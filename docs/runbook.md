# Runbook

All commands run from `~/CLipping` on the Pi unless noted. `cf` below means `.venv/bin/clipforge`.

## Start / stop / status
```bash
systemctl --user status 'clipforge-*'           # all four services
systemctl --user restart clipforge-web          # after editing .env or templates
systemctl --user restart clipforge-worker-light clipforge-worker-render
systemctl --user stop 'clipforge-*'             # stop everything
journalctl --user -u clipforge-worker-light -f  # follow a service's logs
cf status                                       # queue counts, budget, recent errors
curl -s http://127.0.0.1:8420/health | python3 -m json.tool
```
The services are systemd **user** units with lingering enabled, so they start at boot without a login. Unit files live in `deploy/systemd/` and are installed to `~/.config/systemd/user/`.

## Install or update
```bash
git pull                 # if you use a remote
scripts/install-pi.sh    # uv sync, DB migrate, copy units, restart; safe to re-run
uv run pytest -q         # optional: full test suite (~1 min)
```
Schema changes are additive (`CREATE TABLE IF NOT EXISTS`). A column change needs a manual `ALTER TABLE`, and DECISIONS.md will say so.

yt-dlp breaks when YouTube changes things. Update it with `uv lock --upgrade-package yt-dlp && scripts/install-pi.sh`.

## Add a campaign
- **UI:** Campaigns → Add campaign.
- **CLI:** `cf campaign add "Pod X" --rate 40 --per 100000 --category podcast --min-views 10000 --cap 200 --tags "@podx #podxclips"`
- If the campaign supplies a YouTube channel, add its RSS feed (`https://www.youtube.com/feeds/videos.xml?channel_id=UC…`) as a watch feed. The scheduler checks it every 30 minutes.
- Gambling/casino campaigns are refused by the blocklist (`CF_BLOCKED_KEYWORDS`, `CF_BLOCKED_CATEGORIES` in `.env`, as JSON lists).

## Add footage
- **UI:** Sources page (several links, one per line).
- **CLI:** `cf source <campaign_id> <url> [<url>…]`
- **Footage you downloaded by hand** (e.g. from a campaign Drive folder): paste the file's full path. It's used where it sits, never copied or deleted.
  - On the laptop: `C:\Users\Easton\Clipping\Campaigns\ManOn\ep4.mp4` (in Explorer: Shift+right-click → "Copy as path"). It's pinned to the laptop automatically.
  - On the Pi: `/home/pi/footage/ep12.mp4`.
  - CLI: `cf source 1 "C:\…\ep4.mp4"`, plus `--on <host>` to choose the machine explicitly.

## Campaign logo (when the brief requires one)
- **UI:** Campaigns → the campaign → choose a PNG → Upload logo. **CLI:** `cf campaign logo 1 ~/Downloads/logo.png`.
- It's burned into the top-right of every clip rendered **after** the upload. Clips already rendered don't change.
- Don't add a logo to campaigns whose brief bans extra logos (GFuel does).

## Back up the database
```bash
scripts/backup-db.sh     # online-safe .backup into data/backups, keeps 14
```
Nightly at 03:30 via cron: `crontab -e` and add
`30 3 * * * /home/pi/CLipping/scripts/backup-db.sh >/dev/null 2>&1`

Copy `data/backups/` off the Pi now and then (e.g. to the laptop). An SD card is not a backup.

Restore: stop the services, `cp data/backups/<file>.db data/clipforge.db`, delete `data/clipforge.db-wal` and `data/clipforge.db-shm` if present, start the services.

## Rotate keys
1. Create the new key in the provider console.
2. Edit `.env` on the Pi (and on the laptop for Groq / the worker token).
3. `systemctl --user restart 'clipforge-*'`, and restart the laptop worker.
4. Revoke the old key.

Worker token: generate with `openssl rand -hex 24`. It must match on the Pi and the laptop.

## Laptop worker (Windows 10)
1. Copy the repo to the laptop: `git clone pi@raspberrypi:CLipping` over Tailscale SSH, or a zip.
2. In PowerShell, from the repo: `powershell -ExecutionPolicy Bypass -File scripts\laptop\setup.ps1`. It installs uv and ffmpeg and creates `.env`.
3. Paste `CF_WORKER_TOKEN` (from the Pi's `.env`) and `CF_GROQ_API_KEY` into the laptop's `.env`.
4. Double-click `scripts\laptop\start-worker.bat`. Leave the window open; close it to stop.

The Pi dashboard shows "Laptop worker: online".

**Updating the laptop:** after the code changes, the Pi refuses old laptop workers. The worker window then logs `426 Upgrade Required` every 30 s. Stop the worker, unzip the new `clipforge-laptop.zip` over the old folder (it never contains `.env`, so yours is kept), run `uv sync` in the folder, then start the worker again. New downloads now land on the laptop, and renders for those sources run there. To start it at login, put a shortcut to the `.bat` in `shell:startup`.

## A/B prompt versions
- Prompts are the files `clipforge/prompts/select-*.md`. Editing a file creates a new version on the next service restart.
- To change the traffic split:
  ```bash
  sqlite3 data/clipforge.db "UPDATE prompt_versions SET weight=0 WHERE name LIKE 'select-v1-%'"
  ```
- Compare versions under Dashboard → By prompt version. Wait for 30+ reviewed clips per version before judging.

## Budget
- `CF_MONTHLY_BUDGET_USD` in `.env` (default 15). Paid jobs pause when the month's spend reaches it, with a Discord alert at 80% and 100%.
- Raise the cap and restart the workers to resume. Deferred jobs retry every 6 h, or immediately via Sources → Retry.

## Troubleshooting
| Symptom | Check / fix |
|---|---|
| UI unreachable from phone | Is Tailscale up on the Pi and phone (`tailscale status`)? Does `CF_BIND_HOST` match `tailscale ip -4`? Then `systemctl --user restart clipforge-web`. |
| Source stuck at `new` | `cf status`. A fetch error from yt-dlp usually means it's outdated (see Install or update) or the video is private or age-gated. |
| Stuck at `fetched`, error "CF_GROQ_API_KEY is not set" | Add the key (BLOCKERS B3). Jobs retry automatically. |
| Stuck at `transcribed`, error "CF_ANTHROPIC_API_KEY…" | BLOCKERS B2. |
| "monthly budget … reached" | See Budget above. |
| "raw media cap reached" / "only N GB free" | `cf cleanup`, lower `CF_RAW_RETENTION_HOURS`, start the laptop worker, or raise `CF_MEDIA_CAP_GB` if the disk allows. |
| Laptop shows offline | Is the worker window open? Can the laptop reach the Pi: `curl http://raspberrypi:8420/health`? Do the tokens match? |
| Renders for a source never finish while the laptop is off | Its media lives on the laptop (Sources page shows "media on laptop"). Turn the laptop on, or Retry after deleting the source's jobs. |
| "database busy" errors | Rare under WAL. Make sure no one else holds `sqlite3` open with a write transaction. |
| Captions show the wrong font | `fc-scan assets/fonts/Montserrat-ExtraBold.ttf` should list "Montserrat ExtraBold"; `CF_FONT_NAME` must match. |
