# ClipForge

ClipForge is a self-hosted pipeline for **paid clipping campaigns**: brands and streamers (on marketplaces like Content Rewards and Clipping.net) pay per view for short vertical clips of their podcasts, interviews and live streams. ClipForge does the tedious part. It downloads or records the campaign's footage, transcribes it, has Claude pick the moments worth clipping, and renders ready-to-post 9:16 videos. A person reviews every clip on their phone and posts it by hand.

```
footage (campaign folder, YouTube link or a live Kick/Twitch stream)
  → download or record → transcribe (Whisper, word timings) → Claude picks moments
  → render 9:16 (ffmpeg) → Discord ping → review on phone → post by hand
  → log the post URL and views → dashboard, payouts, and feedback to the next picks
```

It runs on two machines: a **Raspberry Pi 5** that is always on (the brain: database, web UI, scheduler, AI calls) and a **Windows laptop** (the muscle: big downloads, live stream recording, rendering). The phone is the screen. All three talk over a private [Tailscale](https://tailscale.com) network, and nothing is exposed to the internet.

> **Human in the loop by design.** ClipForge never posts on its own. It can drop a draft into your TikTok inbox when you tap a button, and you publish it yourself. See [Ground rules](#ground-rules).

---

## What it does

**Finding work**
- **Scout:** upload screenshots of a marketplace's campaign list. Claude reads every bounty card, and ClipForge ranks them by *expected dollars per clip*: rate, minimum and maximum payout, budget left, how fast the budget is filling, marketplace fees, and the odds your clips reach each view tier.
- **Briefs:** paste a campaign's brief or upload screenshots of its requirements. ClipForge reads the terms (rate, minimum views, allowed platforms, required tags and names, clip length, logo rules, what footage is allowed) and then only asks you for what the brief didn't say.

**Making clips**
- **Footage from anywhere the campaign allows:** files you drop into `Campaigns/<campaign>/<bounty>/` on the laptop, YouTube links, a YouTube channel watched for new uploads, or a live stream recorded while it's happening.
- **Transcription:** Groq's Whisper (`whisper-large-v3-turbo`) with word-level timestamps.
- **Moment picking:** Claude (Haiku by default) reads the transcript together with the campaign brief, who is on screen, the names a caption must mention, and your past approve/reject reasons. It returns scored candidates with a hook, a title and a reason, as strict JSON. Prompts are versioned by content hash and A/B tested.
- **Rendering:** ffmpeg builds a 1080×1920 video with a chosen layout (blurred fill, center crop, speaker/facecam/split), loudness normalized to −14 LUFS. Per campaign it can add subtitles under the picture, a hook card, campaign logos or a corner watermark.
- **Live streams:** watches Kick and Twitch channels, records them on the laptop, and clips them in 5-minute chunks while the stream is still going, so a clip can be ready about 5–8 minutes after the moment.

**Reviewing and posting**
- **Phone-first UI:** a "Needs you" home screen, review one clip at a time (approve, or reject with a reason), and a Post page that copies the caption (`#ad` first, required tags checked), saves the video to Photos, or sends a TikTok inbox draft.
- **Tracking:** post URLs, view snapshots, the brand's review status, and payouts after marketplace fees, against each bounty's minimum and cap. Rejection reasons and view counts feed back into the next picks.
- **Discord notifications** (webhook only): clips ready, a streamer went live, posting reminders, deadlines, budget warnings.

**Cost control**
- Every paid API call, failed ones included, is logged with its cost. A monthly budget cap pauses paid work instead of failing it. A typical podcast episode costs a few cents to transcribe and pick; a 5-minute live chunk costs about $0.005.

---

## How the two computers work together

```
        ┌─────────────────────────── Phone (Safari, on the tailnet) ───────────────────────────┐
        │   Home · Campaigns · Lives · Scout · Stats      review → approve → copy caption/post │
        └──────────────────────────────────────▲──────────────────────────────────────────────┘
                                               │ HTTPS via Tailscale Serve (tailnet only)
┌──────────────────────── Raspberry Pi 5 · always on · "the brain" ─────────────────────────────┐
│  clipforge-web (FastAPI + Jinja)  ◄── UI pages and the token-protected /api/worker/* API ◄─┐  │
│  SQLite (WAL): campaigns, bounties, sources, transcripts, candidates, clips, posts, jobs…   │  │
│  clipforge-scheduler: who's live? new YouTube uploads? reminders, cleanup, budget           │  │
│  clipforge-worker-light: select (Claude), scout, brief reading (API keys stay on the Pi)    │  │
│  clipforge-worker-render: renders when the laptop is away                                   │  │
└──────────────────────────────────────────────────────────────────────────────────────────── │ ─┘
                                                                                             │
                     pull jobs, push results, upload finished clips (HTTP over Tailscale) │
                                                                                             │
┌──────────────────────── Windows laptop · "the muscle" ────────────────────────────────────┴──┐
│  clipforge remote-worker:   fetch (yt-dlp) · transcribe (Groq) · render (ffmpeg)             │
│  recorder thread:           records live streams as .ts pieces → 5-min .ffconcat chunks       │
│  library scan:              reports the videos in Campaigns/<campaign>/<bounty>/ to the Pi    │
│  big disk:                  raw footage and recordings never leave this machine               │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

**The Pi never connects to the laptop.** The laptop *pulls*: it asks the Pi for a job (`POST /api/worker/claim`), does it, and posts the result back. So the laptop can sleep, reboot or leave the house, and the Pi, the UI and your phone keep working. When the laptop is offline, the Pi does everything itself under a small disk cap.

**Heavy files stay where they are.** Whichever machine downloads a video keeps it, and that video's transcribe and render jobs are pinned to that machine (job *affinity*). A 4 GB episode never crosses the network. Only the finished clip (10–20 MB) is uploaded to the Pi, which serves it to your phone.

**Saving footage.** Drop campaign videos into the laptop's `Campaigns/<campaign>/<bounty>/` folder; ClipForge creates these folders when you add a campaign or bounty. The laptop reports what's there. The bounty page on your phone shows the files, the estimated cost and render time, and you confirm before anything runs. Files are clipped in place and never moved or deleted.

**Watching live streams.**
1. The Pi's scheduler checks watched Kick/Twitch channels every 2 minutes using public pages (no login).
2. When a streamer goes live, the Pi opens a session and pings Discord. The laptop's recorder sees it on its next check-in, records the stream with ffmpeg (`-c copy`, no re-encode) as 30-second pieces, and every 5 minutes writes a `.ffconcat` chunk that overlaps the previous one by a minute, so no moment is cut in half.
3. Each chunk becomes a source. The laptop transcribes it, the Pi has Claude pick moments (with a live-specific prompt), and the laptop renders the clips.
4. Clips appear on the phone's **Lives** tab, on a timeline of the stream. Streams whose title names a gambling site are recorded but never clipped. Recordings are deleted 72 hours after the stream ends.

**Showing it on the phone.** The web UI is bound to the Pi's Tailscale IP and published inside the tailnet over HTTPS with `tailscale serve` (a real certificate; HTTPS is needed for the iOS share sheet that saves clips to Photos). Only devices signed in to your tailnet can open it.

**Keeping the code in sync.** The laptop never needs a `git pull`. Before each start it asks the Pi which commit it's running and downloads that exact code as a zip (`/api/worker/code`). If the Pi is updated while the laptop runs, the Pi answers with HTTP 426, and the laptop exits, updates and restarts itself. Deploying is: commit on the Pi, restart the services.

---

## Ground rules

These are enforced in code, not just written down:

- **No automated posting.** The only exception is uploading a draft to the owner's TikTok inbox through TikTok's official API, at a tap; the person publishes it.
- **Only footage the campaign supplies or permits.** Every source belongs to a campaign. No scraping behind any marketplace's login or bot checks; campaign info comes from your own screenshots or manual entry.
- **FTC disclosure on every post:** `#ad` first in the caption (checked before you can copy it), plus the platform's paid-partnership toggle.
- **Gambling, casino, sportsbook, poker and prediction-market campaigns are blocked** by default. Political campaigns need the brief to name the sponsor and disclosure terms.
- **Discord is a webhook only**, never a bot or a self-bot.
- **Secrets live only in `.env`**, which is gitignored. [`.env.example`](.env.example) is the template.

---

## Setup

### Requirements
- Raspberry Pi 5 (or any Linux box) with Python 3.11+, ffmpeg and [uv](https://docs.astral.sh/uv/)
- Optional: a Windows 10/11 laptop as the worker
- A [Tailscale](https://tailscale.com) account with both machines and your phone signed in
- API keys: [Groq](https://console.groq.com) (transcription) and [Anthropic](https://console.anthropic.com) (clip selection)
- Optional: a Discord webhook URL; a TikTok developer sandbox app for inbox drafts

### Pi
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # once
git clone https://github.com/EastonShaum/ClipForge.git CLipping && cd CLipping
cp .env.example .env                                 # fill in keys, token, Tailscale IP
scripts/install-pi.sh                                # deps, database, systemd user services
tailscale serve --bg --https=8443 8420               # HTTPS inside your tailnet
```
Set `CF_BIND_HOST` to the Pi's Tailscale IP (`tailscale ip -4`), never `0.0.0.0`. Generate the worker token with `openssl rand -hex 24`.

### Laptop (optional)
```powershell
# In PowerShell, from a copy of the repo (or a zip of it from the Pi):
powershell -ExecutionPolicy Bypass -File scripts\laptop\setup.ps1   # uv, ffmpeg, deps, .env
scripts\laptop\start-worker.bat                                     # self-updates, then runs
```
Paste the same `CF_WORKER_TOKEN` as the Pi into the laptop's `.env`.

### Try it without any keys
```bash
uv sync
CF_TRANSCRIBE_PROVIDER=fake CF_SELECT_PROVIDER=fake uv run pytest -q   # 142 tests, offline
uv run clipforge bench                                                  # render speed here
```

### Everyday commands
```bash
uv run clipforge --help             # init, campaign add/list, source, status, serve, worker,
                                    # remote-worker, scheduler, cleanup, bench
uv run clipforge status             # queue counts, budget, recent errors
systemctl --user status 'clipforge-*'
```

---

## Configuration

All settings are `CF_*` environment variables read from `.env` ([`clipforge/config.py`](clipforge/config.py)). The main ones:

| Variable | What it sets |
|---|---|
| `CF_BIND_HOST`, `CF_PORT` | Where the UI listens (the Pi's Tailscale IP, port 8420) |
| `CF_HTTPS_BASE_URL`, `CF_PUBLIC_BASE_URL` | The tailnet HTTPS address; where Discord links point |
| `CF_WORKER_TOKEN` | Shared secret between the Pi and the laptop worker |
| `CF_GROQ_API_KEY`, `CF_ANTHROPIC_API_KEY` | Transcription and selection keys |
| `CF_SELECT_MODEL` | Claude model for picking moments (default `claude-haiku-5-5`) |
| `CF_MONTHLY_BUDGET_USD` | Paid work pauses when this month's spend reaches it |
| `CF_DISCORD_WEBHOOK_URL` | Notifications |
| `CF_TIKTOK_CLIENT_KEY`, `CF_TIKTOK_CLIENT_SECRET`, `CF_TIKTOK_REDIRECT_URI` | TikTok inbox drafts |
| `CF_LIVE_CHUNK_MIN`, `CF_LIVE_LAYOUT` | Live chunk length; crop, full width or auto per clip |
| `CF_BLOCKED_KEYWORDS`, `CF_BLOCKED_CATEGORIES` | The gambling blocklist |
| `CF_TIMEZONE`, `CF_POST_SLOTS`, `CF_DIGEST_TIME` | Reminder times |

---

## Tech stack

| Part | Choice |
|---|---|
| Language and packaging | Python 3.11+, uv |
| Web | FastAPI, Jinja templates, plain CSS and a little JS (no frontend framework) |
| Database | SQLite in WAL mode, with a job queue built on it (dedupe keys, leases, retries, deferral) |
| Media | ffmpeg and ffprobe, libass for subtitles, Pillow for hook cards, yt-dlp for downloads and live HLS |
| AI | Groq Whisper (transcription), Anthropic Claude via structured outputs (selection, Scout, briefs) |
| Services | systemd user units on the Pi; a batch-file loop on Windows |
| Network | Tailscale (WireGuard mesh), `tailscale serve` for HTTPS |

---

## Project layout

```
clipforge/            Python package
  stages/             fetch, transcribe, select, render, layout, brief, scout: pure work, no DB access
  pipeline.py         builds each job's context and applies its result, for the Pi and the laptop
  jobqueue.py         enqueue / claim / complete / fail / defer
  worker.py           local worker loop and the remote (laptop) HTTP worker
  live.py, recorder.py  live streams: the Pi side and the laptop's recorder
  library.py          the laptop's Campaigns folders
  update.py           laptop self-update from the Pi
  web/                FastAPI app, templates, CSS
  prompts/            versioned prompts (select-*.md, live-*.md)
tests/                pytest suite, fully offline with fake providers
scripts/              Pi install and backups; scripts/laptop/ for the Windows worker
deploy/systemd/       the four Pi services
docs/                 architecture, runbook, launch playbook
research/             sourced research on platforms, payouts, hooks and account growth
DECISIONS.md          why it's built this way (D1–D48)
```

## Docs
- [docs/architecture.md](docs/architecture.md): data flow for one video, code map, job states
- [docs/runbook.md](docs/runbook.md): operating, backups, troubleshooting
- [docs/launch-playbook.md](docs/launch-playbook.md): account warm-up and the daily routine
- [research/SUMMARY.md](research/SUMMARY.md): what the research found
- [DECISIONS.md](DECISIONS.md): every design decision and the reasons behind it

## Status

A personal project in daily use: clips podcasts and live streams for Content Rewards campaigns. It's built for one operator on their own hardware, not as a hosted service. 142 tests pass.
