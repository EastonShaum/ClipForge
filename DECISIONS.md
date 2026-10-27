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

## D19. Campaign logos: burned in as a "TV bug", required means required
- **Why:** campaign briefs (e.g. Man On) require the brand logo to be "CLEARLY VISIBLE". Other briefs (e.g. GFuel) ban any logo that isn't required, so the logo is per campaign and off by default.
- **Placement:** 210 px wide, top-right corner of the 16:9 picture (y≈556 on the 1080×1920 canvas). That's below the hook box and the disclosure, and above where TikTok's right-hand buttons start. Captions draw on top of it.
- **Storage:** the upload is shrunk once to a 480 px PNG with alpha under `data/assets/logos/c<id>-<sha>.png`. The content hash in the name means a replaced logo can never be served from a stale laptop cache.
- **Failure mode:** if a campaign has a logo and the file is missing on the render machine, the render **fails**. A clip without a required logo gets disqualified, so a loud error is better than a quiet skip.
- **Rejected:** putting the logo in the ASS subtitles (they can't do images); a corner of the full canvas (it collides with platform UI); asking you to add it in an editor by hand (it doesn't scale).

## D20. Local footage is used in place; Windows paths go to the laptop
- **Why:** campaign footage often comes from Drive folders that hit download quotas, so you download it by hand. Man On ep-4 is a 4.85 GB 4K file, and the Pi had 4.8 GB free.
- **How:** a source can be a path (`C:\…`, `/home/…`, or `file://…`). The fetch stage probes it with ffprobe and records the path. Nothing is copied, and raw-media cleanup only deletes `data/raw/<id>`, so your original file is never deleted.
- **Pinning:** the fetch job is pinned to the machine that has the file. A Windows path means the most recently seen laptop worker; a POSIX path means the Pi; `--on HOST` (CLI) or the Sources form overrides. Transcribe and render follow automatically through media affinity (D5).
- **Worker protocol:** remote workers send `protocol: 2`. The Pi answers older workers with HTTP 426 and doesn't count them as online. Otherwise an outdated laptop would render Man On clips without the logo.
- **4K:** no 1080p proxy. The laptop decodes 4K fast enough, and the renderer scales down anyway. If the Pi ever has to render 4K, it's slow but correct.

## D21. The laptop updates itself from the Pi
- **Why:** two copies of the code drift. A stale laptop rendered nothing useful once already, and could render clips without a required logo.
- **How:**
  - The Pi records the git commit it loaded.
  - Over the token-protected worker API it serves that commit (`/api/worker/code/version`, and `/api/worker/code` as a `git archive` zip).
  - `start-worker.bat` runs `clipforge self-update` before every start. That replaces code files but never `.env`, `data/` or `.venv`.
  - The Pi answers a worker on a different commit with 426. The worker exits with code 3, and the script loops back to update and restart.
- **Deploy rule:** commit, then `systemctl --user restart 'clipforge-*'`. Uncommitted edits on the Pi aren't served to the laptop.
- **Also:** the worker locates ffmpeg/ffprobe itself (`tools.py`). winget's PATH change doesn't reach windows opened before you sign out and back in. The worker refuses to start if they're missing, instead of failing every job.
- **Rejected:** GitHub plus `git pull` (needs an account and a push step, and the code leaves your tailnet); syncing folders with Syncthing (it would also sync half-written files, and nothing would check versions).

## D22. Discord reminders for posting, deadlines and link submission
- **Why:** posting stays manual, so the risks are forgetting: missing a slot, posting to only one of a clip's platforms, a bounty ending with clips unposted, or a posted link never submitted to the campaign (no submission, no pay).
- **How:** the scheduler calls `reminders.tick` every minute.
  - **Post slots** (`CF_POST_SLOTS`, local time in `CF_TIMEZONE`): names the next clip and the platforms it still needs. Clips from the campaign ending soonest come first, then the highest score.
  - **Morning digest** (`CF_DIGEST_TIME`): campaigns ending within 3 days, plus post links not yet marked submitted. It repeats daily until you tap "Mark submitted" on the Post page.
  - Each reminder fires once (a `settings_kv` key). A slot missed by more than 2 h is dropped, so a reboot doesn't replay old pings.
- **Timing:** slot times are a starting point, not a science. Sprout Social's 2026 data puts TikTok's best window at Tue–Fri 2–6 pm local. A new account's first views come from a test audience, so consistency matters more than the minute. Re-tune from your own analytics after ~30 posts.
- **Rejected:** scheduling the posts themselves (no automated posting, hard constraint 2).

## D23. Campaign logos are a row under the captions, and a campaign can have several
- **Why:** Man On requires the Man On logo *and* the Playmaker wordmark. The footage also carries its own Playmaker mark in the top-right corner, which the D19 "TV bug" placement covered.
- **How:** `campaigns.logo_path` holds every logo, comma-separated, in display order (`logo_list()`). The renderer scales each to 120 px tall and lines them up side by side on a 55% black strip at y≈1300–1444. That's the blurred band under the captions, centered on the caption column and above TikTok's bottom UI. The strip keeps white logos readable on any background.
- **Worker protocol 3:** ctx carries `logo_rels` (a list). Older laptops get 426 and self-update.
- **Rejected:** stacking logos in the top-right of the picture (hides the footage's own marks and fights the hook box); a per-logo position setting (nothing needs it yet).

## D24. Scout: rank bounties from screenshots you upload
- **Why:** choosing the right bounty matters more than any render setting, and Clipping.net can't be fetched (Vercel bot check, which we don't bypass). Screenshots taken by hand are allowed and contain everything on the campaign page.
- **How:** the Scout page takes many screenshots at once from the phone. Each is shrunk to ≤1568 px JPEG (named by hash, so duplicates are ignored) and becomes a `scout:<id>` job on the Pi. Haiku reads the bounty cards into fixed fields; cards cut off at the edge are skipped. That costs about $0.0008 per screenshot, logged in `api_costs` and subject to the budget cap.
- **Ranking** (`clipforge/scout.py`), a plain formula: pay rate 40 + budget left 35 + content fit 15 + PayPal 10. Pot bounties score low on pay. Gambling is blocked; ended and disabled bounties are listed apart. Uploading again a day later gives the fill speed in %/day and a "full in ~N days" estimate, with −15 if a bounty will close within 2 days.
- **Schema note:** the extraction model has no Optional fields (structured outputs rejected the schema as too complex); numbers use −1 for "not shown".
- **Not automated:** nothing visits Clipping.net and nothing adds a campaign by itself. "Add as campaign" only pre-fills the form.

## D25. Requirement screenshots attach to a campaign and are applied on request
- **Why:** briefs hold the rules that disqualify clips (tags, logos, platforms, banned content), and their docs often sit behind the same bot check as the campaign pages.
- **How:** on a campaign card, "Upload requirement screenshots" stores each image (deduplicated per campaign) and queues a `brief:<doc_id>` job on the Pi. Haiku reads the tags, platforms, length, end date, footage links, logo rule and other rules (~$0.0008 each, logged). The card shows them merged across screenshots.
- **Apply to campaign** only adds or fills in: tags, platforms, end date, empty caption text, links, and rules appended to notes. It never removes anything and never touches the disclosure; if the brief names its own wording, the card shows it for you to add.
- **Not yet:** the selector prompt doesn't see brief rules (e.g. "mention G FUEL on screen"). That comes with the GFuel flow.

## D26. One page per campaign; footage comes from the laptop's Campaigns folder, confirmed first
- **Why:** you download big footage sets (Brandon Joe Williams: 80 GB) to the laptop. Pasting paths one by one doesn't scale, and a typo or a stray file means paid transcription of the wrong video.
- **How:**
  - Every campaign gets its own page: footage, requirements, logos, settings, delete. The Sources tab is gone (`/sources` redirects).
  - New campaigns get `media_folder` = the name minus Windows-forbidden characters. On each scan (every 60 s, between jobs) the laptop worker asks `/api/worker/library/folders` and creates any missing folder under `media_library_dir` (`<repo>/Campaigns`, kept out of git and out of self-updates).
  - The worker reports every video file there (path, size, mtime, ffprobe duration) to `/api/worker/library`, only when something changed. The Pi replaces that host's rows in `library_files`.
  - Campaigns without a set folder (Man On) match a folder by name.
  - Files modified in the last 2 minutes or that ffprobe can't read count as "still downloading" and can't be queued. Chrome's `.crdownload` files aren't listed at all.
  - **Queue footage** shows a confirmation page first: files with checkboxes, hours, estimated API cost against the budget left, clips to review and laptop render time. Only **Confirm** creates sources, each pinned to the host that has the file.
- **Delete campaign:** removes the campaign and all ClipForge rows and files for it (sources, clips and their files, posts, views, jobs, brief screenshots, logos). It never touches your footage, keeps `api_costs` (money spent is history), and refuses while one of its jobs is running.
- **Theme:** dark by default, with a ◐ switch in the nav that each device remembers (localStorage).
