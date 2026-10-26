# Decisions

Why things are the way they are, including the alternatives I rejected. Newest at the bottom.

## D1. Stack: Python 3.11+, FastAPI, SQLite (WAL), uv
- **Why:** everything the pipeline touches (yt-dlp, ffmpeg wrappers, Whisper, the Anthropic SDK) is Python-first. SQLite is a single file, needs no server process on the Pi, and backs up with one command.
- **WAL mode** lets the web UI read while a worker writes. Without it, readers block writers and you get "database is locked" errors.
- **Rejected:** Postgres (another daemon to babysit on a Pi for no benefit at this scale), Redis/Celery (a second source of truth for job state; SQLite already gives us transactions).

## D2. Server-rendered HTML forms, no JS framework, no htmx
- Every action (approve, reject, log post) is a plain `<form method=post>` followed by a redirect. It works on any phone browser, has no build step, and nothing loads from a CDN.
- **Rejected:** React/Vue (a build pipeline for 6 pages) and htmx (nice, but plain forms were already enough).
- The only JavaScript is a copy-to-clipboard helper that falls back to `execCommand` because `navigator.clipboard` needs HTTPS (see D18).

## D3. Two kinds of "state": entity status vs. the work queue
The brief described one `jobs` table moving through `new → fetched → … → posted`. I split that in two:
- **Entity status** (`sources.status`, `clips.status`) answers "where is this video/clip in its life?" It's what the UI shows.
- **Jobs** (`jobs` table) answer "what work is waiting, running, or failed, and who holds it?" One row per unit of work (`fetch:12`, `render:57`).
- **Why:** one source fans out into many clips, so a single state column can't describe both "source is transcribed" and "3 of 6 clips rendered". Retries, leases and backoff belong to the work, not to the video.

## D4. Queue mechanics: dedupe keys, leases, idempotent handlers
- `dedupe_key` is UNIQUE, so `INSERT OR IGNORE` makes enqueueing safe to repeat.
- **Claiming** runs inside `BEGIN IMMEDIATE`, which takes SQLite's write lock up front, so two workers can never grab the same row.
- **Leases:** a claimed job gets `locked_until = now + 30 min`. If the worker dies (power cut, laptop lid closed), the lease expires and another worker re-claims it. That's why **every handler is idempotent**:
  - fetch reuses a finished download;
  - transcript and candidate inserts use `ON CONFLICT DO NOTHING`;
  - candidates are keyed on `(source_id, start_ms, end_ms)`;
  - clips are keyed on `(candidate_id, preset)`;
  - renders write to `*.part.mp4` and then rename atomically.
- **Failures** retry with backoff (1 m, 5 m, 15 m, 1 h, 3 h), then the job goes `dead` and the source shows as `failed` with the error and a Retry button.
- **Deferrals** (budget cap, disk full, missing API key) re-queue *without* burning an attempt, because waiting fixes them; retrying doesn't. `tests/test_e2e.py::test_rerun_is_idempotent` proves re-running every job creates no duplicates.

## D5. Pi is the brain, laptop is optional muscle (pull model)
- The Pi owns the DB, the UI and the scheduler. The laptop runs `clipforge remote-worker`, which **pulls** jobs over HTTP (`/api/worker/claim`) and pushes results back. The Pi never connects to the laptop, so the laptop can sleep, update or be away.
- **Media affinity:** whoever downloads a source keeps its raw file. The transcribe and render jobs for that source are pinned (`affinity_host`) to that machine, so the 1–3 GB raw files never cross the network. Only finished clips (~10–20 MB) are uploaded.
- While the laptop is online (it polled within 10 min), the Pi leaves new, unpinned **fetch** jobs to it, so big downloads land on the laptop's 758 GB drive. When the laptop is offline, the Pi does everything itself under a 5 GB raw-media cap.
- **Why not run everything on the laptop?** The review page and Discord links would die whenever it sleeps, reboots or installs updates, and Windows 10 stopped getting security updates in Oct 2025, so it shouldn't be an always-on server.
- **Selection always runs on the Pi** (`select` jobs are pinned to the Pi's host name), so the API key lives in one place.

## D6. Render benchmark → default placement
Measured on this Pi 5 (4 cores, 8 GB): a 45 s clip rendered to 1080×1920 with burned-in captions, from a synthetic 1080p30 source:

| Layout | x264 preset | Wall time | Realtime | Size |
|---|---|---|---|---|
| blurfill | veryfast | 54.0 s | 0.83× | 10.7 MB |
| crop | veryfast | 38.7 s | 1.16× | 16.7 MB |
| blurfill | ultrafast | 54.7 s | 0.82× | 43.2 MB |

- `ultrafast` doesn't help, so the bottleneck is decoding plus filtering (blur and libass), not encoding. The Pi 5 has no hardware H.264 encoder, so this is the ceiling.
- **Decision:** the Pi renders by default. About 1 minute per clip means 6 clips per source take ~6 minutes, which is fine for a review-later workflow. The laptop is a bonus, not a requirement.
- Render runs in its own low-priority service (`Nice=10`), so it never starves the UI.

## D7. Default layout: blurfill (whole frame over a blurred copy), not center crop
- A blind center crop of a 16:9 two-person podcast often shows half of each face, and in gameplay the action is rarely centered.
- Blurfill shows the full frame, centered slightly above the middle, with a blurred zoom of itself filling the space. That leaves clean bands for the hook (top) and captions (lower middle).
- `CF_RENDER_LAYOUT=crop` is still available. Face-tracking crop is a stretch goal (see PLAN).
- The blur is computed at 270×480 and scaled up, which is about 10× cheaper than blurring at full resolution.

## D8. Transcription: Groq `whisper-large-v3-turbo`, chunked
- $0.04/audio-hour, and the free tier covers about 8 h/day with no card. Word timestamps come back via `timestamp_granularities[]=word`. Source: research/api-costs.md.
- Audio is re-encoded to 16 kHz mono Opus at 32 kbps (~14 MB/h) and sent in 10-minute chunks with 2 s of overlap. Each chunk is ~2.4 MB, far below the 25 MB free-tier limit, and a failure only retries 10 minutes of audio. Words in the overlap are deduplicated by timestamp.
- **Rejected:** OpenAI whisper-1 (9× the price), Deepgram/AssemblyAI (fine but no free tier advantage), and local faster-whisper on the Pi (far too slow). Local faster-whisper on the laptop exists as `CF_TRANSCRIBE_PROVIDER=local`: free but ~1× realtime on CPU.

## D9. Clip selection: Anthropic API (Claude Haiku 5.5) by default
- Cost per 1-hour source is about **$0.004 on Haiku 5.5**, or about $0.08 on Sonnet 5.5. At 5 sources/day that's ~$0.63/month on Haiku and ~$12.60/month on Sonnet (research/api-costs.md).
- **Why not your Pro subscription via `claude -p`?** It works and the code supports it (`CF_SELECT_PROVIDER=claude_cli`, running tool-less with no session and JSON-schema output). But Anthropic's Consumer Terms forbid automated access except via an API key "or where we otherwise explicitly permit it", and nothing explicitly permits an unattended cron job on Pro. Pro limits are also shared with your chat usage. For less than $1/month, the API key is the clean route. Your call; see QUESTIONS.md.
- The LLM returns **seconds**, and the code snaps them to real word boundaries (−150 ms lead-in, +350 ms tail). Clips outside the length limits, or overlapping by more than 5 s, are dropped. The model never has to be precise.
- The transcript is sent as one line per phrase (`[123.4] text`) instead of per-word timestamps. That cuts tokens ~5× and keeps Haiku under its 100K-token price tier.

## D10. Captions: our own ASS (libass) style, not a SaaS template
- Captions are 3 words at a time, uppercase Montserrat ExtraBold with a thick black outline, the active word in yellow at 108% size, placed at about 2/3 of the screen height with a wider right margin for TikTok's button rail.
- Research (build-vs-buy) found campaign reviewers reject recognizable AI caption templates and third-party watermarks, so a custom style is an advantage.
- Montserrat is OFL-licensed; the font and its license are committed under `assets/fonts`.

## D11. Disclosure in two places
- The caption starts with the campaign's disclosure (default `#ad`).
- The video carries an always-on "Paid partnership #ad" tag in the top-left safe zone. The FTC says a platform's toggle alone may not be enough (research/account-ramp-up.md).
- The Post page shows the per-platform toggle reminder (TikTok "Disclose commercial content", YouTube "Paid promotion", Instagram "Paid partnership").

## D12. Discord webhook for notifications, not a bot
A webhook is a URL you create in channel settings. It can only post into that one channel, so it's an official feature, not a self-bot, and needs no bot token. If it isn't configured, notifications go to the log.

## D13. Budget cap = deferral, plus "API-equivalent" cost
- Every paid call writes an `api_costs` row. Before a paid stage runs, `check_budget` compares month-to-date spend plus an estimate against `CF_MONTHLY_BUDGET_USD`. Over the cap, the job is **deferred** (re-tried every 6 h), not failed, so it resumes automatically next month or when you raise the cap.
- Discord alerts fire at 80% and 100%.
- `usd_equiv` records what a call *would* cost on the paid API, even when it was free (CLI/subscription), so the dashboard answers "what will this cost if I switch?"

## D14. Prompt versions are content-addressed; A/B is weighted random
- Every `clipforge/prompts/select-*.md` file is registered as `<stem>-<sha6>`. Editing a prompt creates a new version automatically, and old versions stay in the DB so analytics can still compare them.
- Removing a file deactivates its version.
- Each select job picks an active version at random, weighted by `prompt_versions.weight`.
- v1 (my draft) and v2 (built on the researched rubric) start 50/50. The dashboard's "By prompt version" table compares approval rate and views per post.

## D15. Feedback loop: few-shot text, nothing clever
`feedback.build_feedback` adds up to 6 "LIKED" examples (approved clips, ranked by real views) and 6 "REJECTED" examples (with your rejection reason and note) to the selector prompt. You can read exactly what the model was told. Rejected: embeddings, fine-tuning, or learned scoring weights, which need far more data than one person's reviews produce.

## D16. systemd **user** services, journald logs
- Lingering is enabled for your user, so `systemctl --user` services start at boot without a login and without sudo.
- Logs go to journald (`journalctl --user -u clipforge-web`), which already rotates by size, so no logrotate config is needed.
- Four units: web, worker-light (fetch/transcribe/select), worker-render, and scheduler. Rendering gets its own process so a 5-minute render queue never delays a 30-second selection.

## D17. View tracking: manual first
- The Views page takes a number per post, and each entry is a snapshot.
- Automatic tracking will use only official APIs: the YouTube Data API `videos.list` gives public view counts with a free key. TikTok's Display API only returns your own videos after OAuth plus app review, so it's a later item.
- No scraping, per the hard constraints.

## D18. Network: bind to the Tailscale IP only
- `CF_BIND_HOST` is the Tailscale IP in production (127.0.0.1 until Tailscale is logged back in). It's never 0.0.0.0, and it's never routed through Cloudflare or Caddy.
- **Recommended upgrade:** `tailscale serve --bg 8420` gives `https://raspberrypi.<tailnet>.ts.net` with a real certificate, visible only inside your tailnet. That enables the modern clipboard API and makes copying captions on iOS reliable.
