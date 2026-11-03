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

## D27. Campaigns hold bounties
- **Why:** on Clipping.net a campaign (Man On) pays through several bounties (one per episode), each with its own rate, budget, requirements doc and end date. One ClipForge campaign per bounty duplicated the brand settings and scattered footage.
- **Campaign** (brand): name, category, marketplace, platforms, logos, tags on every clip, disclosure.
- **Bounty:** rate, min views, cap, end date, status, extra tags, notes, links, requirement screenshots, footage folder. Sources belong to a bounty (`sources.bounty_id`; `campaign_id` stays for joins). Payouts, the dashboard rows, "ending soon" reminders and auto-ending use the bounty.
- **Folders:** `Campaigns/<campaign folder>/<bounty>/`, created on the laptop like D26. The campaign folder is its name, or one you already use (Man On → `ManOn`). A default bounty named "main" uses the campaign folder itself. Subfolders inside a bounty folder count (unzipped downloads nest).
- **Moved files:** if a clipped local file's path disappears and exactly one library file has the same name, the source follows it (`library.relink_moved`). Moving footage into its bounty folder doesn't offer it for clipping (and billing) again.
- **Migration:** one-time, in `init_db`. Each existing campaign gets one "main" bounty carrying its rate, end date and folder, and its sources and brief screenshots move to it. The old rate columns on `campaigns` stay but are unused.
- **Adding:** the Add campaign form takes the first bounty. Using an existing campaign's name adds a bounty to it, which is how Scout's "Add as bounty" works. Each campaign page has "Add bounty".

## D28. Footage-ready pings and opt-in auto-queue (2026-10-28)
When the laptop reports a finished video (settled, readable) in an active bounty's folder, the Pi posts one Discord message per batch (keyed in settings_kv, so never repeated). A bounty with **auto_queue** on also queues those files for clipping, skipping the confirm step. Off by default: the confirm step (D26) stays the norm, and auto-queue is for footage you chose to download into that folder, like an overnight Kick VOD. Spend is still bounded by the monthly budget cap.
Clips per video: each bounty has a default (6), the confirm page can override it per batch (1–18, the selector proposes 18), and the select stage keeps that many top-scored candidates. The laptop scan treats a video as still downloading while yt-dlp has a sibling piece (`x.temp.mp4` during its HLS fixup, `x.f137.mp4` before a merge), because the finished-looking `x.mp4` gets replaced at the end.

## D29. Review, post and views live inside each bounty (2026-10-28)
Each bounty page has tabs: **Review · Post · Posted & views · Footage & settings**, each showing only that bounty's clips. The top nav lost Review/Post/Views (Campaigns is the home page, and each bounty row there links to what's waiting). The old `/review`, `/approved` and `/posts` URLs redirect to the bounty with the most clips at that stage, so old Discord links still work; new pings link to the bounty tab directly. A posted clip stays on the Post tab until every allowed platform has a link. On the Posted tab each post's link and platform can be edited, a post can be removed (with its view history; a clip with no posts left goes back to Post), and more links can be added. Views compare against the bounty's minimum, not the campaign's old one.

## D30. Live streams: watch, record on the laptop, clip in chunks, hold renders for the rules (2026-10-28)
- **Why:** the first clips of a live moment get the views. Waiting for the full VOD (N3on × Lizzo) cost hours. Streamers who run campaigns often go live before the bounty is posted, so footage should be ready by then.
- **Watching (Pi):** campaigns get `live_channels` (kick.com/… or twitch.tv/…) and `watch_live`. The scheduler checks watched campaigns every `CF_LIVE_CHECK_S` (120 s) with yt-dlp's Kick/Twitch extractors, which read the public pages without logging in or downloading anything. A campaign with no channels gets its name tried on both sites once ("found by name; check"). Each broadcast is one `live_sessions` row and one 🔴 ping. A stream that drops and comes back within 10 min is the same session. A streamer multistreaming to Kick and Twitch is recorded once.
- **Recording (laptop):** the Pi's disk can't hold streams (3.7 GB free). A thread in the remote worker (`recorder.py`) asks `/api/worker/live` what to record. It gets the HLS URL from yt-dlp and records it with ffmpeg (`-c copy`, no re-encode, ≤ `max_download_height`) as 2-minute `.ts` pieces under `Live/<campaign>/<channel date>/`. That folder is outside the Campaigns library, so recordings never show up as footage.
- **Chunks:** finished pieces are listed in 30-minute `.ffconcat` files. ffmpeg and ffprobe read these as one video, so transcribe and render needed no changes and no disk is duplicated. Each chunk repeats the previous chunk's last piece, so a moment on a boundary is whole in one chunk. Candidates that end inside that overlap are not kept (`sources.skip_ms`). Each chunk becomes a source in the live bounty, pinned to the laptop, keeping `CF_LIVE_CLIPS_PER_CHUNK` (3) clips. After the stream ends, the last scrap becomes a final chunk, then a ⚫ wrap-up ping.
- **Who pays:** a campaign that is only watched is recorded, not clipped: nothing is transcribed or billed until a bounty with **Clip live streams** exists. If one is added (Scout → **Add & clip live**), recordings from the last 24 h join it and are clipped at once.
- **Holding renders:** a live bounty starts with `render_hold`. Its clips are picked, but they render only once the bounty has requirement screenshots that were read and applied, plus a campaign logo if the brief mentions logos and doesn't ban them. Renders start on their own when that's done, or with **Render now**. While clips wait, Discord says what's missing (at once, then every 3 h).
- **Cleanup:** the Pi tells the recorder to delete a recording `CF_LIVE_KEEP_HOURS` (72) after the stream ends, once every chunk source has rendered or failed.
- **Limits:** only the recording laptop can clip a live stream; if it's off, nothing records (the 🔴 ping says so). Twitch may put ads into the start of an anonymous stream. Detection lags by up to 2 min, so the first minutes can be missed.

## D31. Hook card with emojis for the whole clip, no burned-in captions or on-screen #ad (2026-10-29)
- **Why (Easton's call):** TikTok adds its own auto-captions, so burned-in ones doubled up. The hook text was gone after 3 s. Easton wants more emojis, the text visible the whole time, and context (place, event, activity) when the moment needs it.
- **How:** `stages/hookcard.py` draws the hook with Pillow as a white rounded card (Montserrat + color emojis) and the render overlays it for the whole clip, bottom edge at y=1285 where the captions were, with the logos under it. libass can't draw color emoji, which is why it's an image. The emoji font is the system one (Noto Color Emoji on the Pi, Segoe UI Emoji on Windows; `CF_EMOJI_FONT` overrides). Apple's emoji font isn't used: its license only covers Apple devices.
- **Prompts:** both select prompts ask for 6–12 words plus 2–3 emojis, naming the place/event/activity from the transcript when a stranger would need it, never inventing one. Live chunks now carry the stream title (e.g. "N3on x Ryan Garcia Halloween Party") as the source title, and a fetch no longer overwrites a title that's already set.
- **Disclosure:** the burned-in "Paid partnership #ad" label is off (`CF_ONSCREEN_DISCLOSURE=false`). Easton turns on the platform's own paid-partnership/branded-content toggle on every post instead, and the post caption still starts with `#ad`. Turn the setting back on if a campaign or platform asks for the label in the video.

**Update (2026-10-29):** the card covered the bottom of the picture, so it now sits just under the picture (render.py `placement`, from the source's probed size) and the logo strip moved to the band above the picture.

**Update 2 (2026-10-29):** the burned-in card is now off by default (`CF_HOOK_CARD`). Apple doesn't license its emoji designs, so Easton adds the hook with TikTok's own text tool on his iPhone, which draws real Apple emojis. The Post page has a "Copy on-screen text" button for each clip. Turning the setting back on restores the card.

## D32. TikTok drafts go to Easton's inbox; he still publishes every post (2026-10-29)
Easton asked whether ClipForge could post for him. The rule against automated posting is now narrowed: ClipForge may **upload a draft to his TikTok inbox** with the official Content Posting API (`video.upload` scope, no Direct Post). He opens the draft in the app, adds the on-screen text (real Apple emojis, see D31), pastes the caption, turns on the disclosure toggle and posts it himself. Nothing publishes without him.
- **Why a sandbox app:** TikTok's app review rejects apps "for private or personal use", and ClipForge is exactly that. A sandbox app works for up to 10 accounts the developer owns, with no review. It is meant for testing, so TikTok may stop it working; then we go back to the manual Save video flow.
- **OAuth:** Login Kit for web, redirect to `https://raspberrypi.your-tailnet.ts.net:8443/auth/tiktok/callback` (Tailscale serve, tailnet only; port 443 belongs to Pi-hole). Tokens are kept in `settings_kv` (the DB is gitignored); the client key and secret are in `.env`.
- **Upload:** one tap per clip on the Post page (`/clips/{id}/tiktok`), FILE_UPLOAD in 5–64 MB chunks, status checked on demand. The post URL still gets logged by hand.
- The terms and privacy pages TikTok asks for are `/terms` and `/privacy`.
- **Confirmed working 2026-10-29:** the sandbox app connected and clip 23 reached SEND_TO_USER_INBOX.

## D33. Live streams: 5-minute chunks, 6–25 s clips, named people, full-screen crop (2026-10-29)
Easton sent 5 TikToks of N3on's Halloween stream from the top clipper (@pradaclippedit, up to 1.8M views). Matched against our recording, our selector had missed all 4 of the moments it could place. Their clips were 6–18 s, posted 1–9 min after the moment, and named N3on and the guest in every hook. Ours were 15–60 s, up to 40 min late, and said "streamer" or "famous guy".
- **Speed:** the recorder writes 30 s pieces and the Pi clips 5-minute chunks with 60 s of overlap (`live.PIECE_S`, `OVERLAP_S`, `CF_LIVE_CHUNK_MIN=5`). A clip is ready ~5–8 min after the moment. Selection costs ~$0.001 per chunk.
- **Own prompt:** `prompts/live-*.md` is a separate pool from `select-*` (`active_versions(live=True)`), so the A/B split of ordinary prompts is untouched. It ranks the moment types that won (stream breaks, slips on live, confrontations, surprises), asks for 6–25 s (`CF_LIVE_MIN_CLIP_S`/`MAX`), allows an empty answer, and gives the winners' hooks as the style.
- **Names:** each live session has a "who's on stream" field. The prompt gets `{streamer}` (the campaign name) and `{people}`; the caption gets `#n3on` plus a tag for each listed guest the hook names (`live.hashtags`).
- **Layout:** live chunks render with `CF_LIVE_LAYOUT=crop`, full screen like the winners.
- **Check:** a dry run of live-v1 on 5-minute windows of the old chunks found the 1.8M "END stream" moment (its window's top pick, 25 s) and the security moment (11 s); it still missed the flowers and the drunk guest. Worker protocol 5.


## D34. Lives get their own tab, gambling streams aren't clipped, screens keep their width (2026-10-29)
- **Each stream is its own thing.** Before, every stream went into the campaign's one live bounty, so the 10-29 stream's clips landed in "live-10-28". Now each stream gets its own bounty row when it starts (`live.own_bounty`, `bounties.live_session_id`, named "live 10-29"). It has no folder or rules, and it's hidden from the campaign page and the campaigns list. The new **Lives** tab (`/lives`) lists the streams, each with its Review / Post / Posted tabs, the guests field and a "Move" menu. When a paid bounty comes up, Easton moves the stream into it (`live.move_to`). Its clips and later chunks go there, and unposted clips get the bounty's extra caption tags. The same menu moves it back. "Add & clip live" from Scout while the streamer is live moves today's stream in. Nothing attaches on its own any more (`attach` is gone). Watching a streamer now means record **and** clip. Streamers can be added on the Lives tab, and a streamer with no paid bounty doesn't show under Campaigns.
- **Gambling streams are recorded but never clipped.** The 10-29 stream was titled "JACK.COM 18+ GIVEAWAY IF WIN" and showed slots. 3 clips of it had reached Review; they were taken out, and their rejections are kept out of the selector's feedback, since the picks weren't the problem. A stream whose title matches `CF_BLOCKED_KEYWORDS` (now also jack.com, blackjack, roulette, gambling) gets `live_sessions.blocked`. Its chunks aren't queued, and it stays blocked if it's retitled. The check is by title only; a casino stream with a clean title would still need Easton to catch it in Review.
- **Layout per clip:** `CF_LIVE_LAYOUT=auto`. The render samples 5 frames of the clip (`stages/layout.py`). A shared screen (a Discord call, a browser, a game) is made of flat panels, so if the median frame has ≥12% of its pixels in long flat runs along a row, it keeps the whole width (blurfill). Otherwise it's camera footage and gets the full-screen crop. Measured: screen frames 0.22–0.46, IRL 0.00–0.06, including the dark night scenes. Worker protocol 6.

## D35. Redesign: a "Needs you" home, review one clip at a time, payout progress, stream timelines (2026-10-30)
Easton reviewed three mockups (Cutting room, Thumb zone, Scoreboard) and picked the recommended mix. The old look was a near-black page with one green accent, every section in the same card, and small tap targets.
- **Look:** light by default (dark stays, switched on the Stats page). Bricolage Grotesque for headings and numbers, Figtree for text, signal blue for actions, burnt orange for reject. Tap targets are at least 44 px, focus rings are visible, and pages honor reduced motion and the iPhone safe areas.
- **Bottom tab bar:** Home, Campaigns, Lives (with a dot while someone is live), Scout, Stats.
- **Home (`/`, `inbox.py`):** "Needs you". The bounty with the most clips to review is the big card. Then the rest to review, to post, posts whose views weren't logged in a day, and links not yet submitted on a paid bounty. Then streams that are live now.
- **Review:** one clip at a time, best first ("1 of 8"). Reject, Skip and Approve stay at the bottom; reject reasons open in a bottom sheet (`<dialog>`).
- **Payout progress** on the Campaigns list and each bounty's header: the best clip's views against the bounty's minimum (`inbox.progress`).
- **Lives:** each stream is a timeline with a mark per clip, colored by stage (`live.timeline`, placed from the chunk name's start time).
- The skills these came from are in `.claude/skills/` (frontend-design from Anthropic, web-design-guidelines from Vercel).

## D36. Clip shape per streamer (2026-10-30)
- The auto layout (D34) looks for flat screen panels. A 3D game (BurntPeanut's Dead by Daylight) has none, so it read as a camera and got cropped to the middle. yt-dlp doesn't report the Twitch category, so the stream can't tell us either.
- Each streamer on the Lives tab has a "Clip shape": Auto (default), Full width (games, screens) or Crop to the middle (IRL). Stored in `campaigns.live_layout` (NULL = `CF_LIVE_LAYOUT`) and passed in the render context. BurntPeanut is set to Full width. No worker protocol bump: workers already know `blurfill` and `crop`.

## D37. Stream on/off, Lives tabs, local times, and a Files page (2026-10-31)
- **On/off per live stream:** an On/Off switch on each live stream. Off (`live_sessions.stopped_at`) tells the laptop to stop recording, cancels chunk jobs not started yet (job state `cancelled`) and queues no new chunks. Clips already made stay. On resumes into the same folder.
- **Lives** has Streams and Streamers tabs at the top. Ended streams fold to one line: the streamer, the day and start–end times.
- **Times** show in `CF_TIMEZONE` (America/Denver: MDT, MST from Nov 1) through the `local` Jinja filter. The DB stays UTC.
- **Fix: old recordings were never deleted.** The purge waited for every chunk's source to be `rendered`, but a chunk with no good moments stays `selected`, so no stream ever qualified. Now a finished stream is deleted `live_keep_hours` after it ends unless chunk jobs are still pending or picked clips are waiting to render (`live.WAITING_JOBS`, `WAITING_CLIPS`).
- **Files page (`/files`, `storage.py`):** stream recordings on the laptop, downloaded videos and clip videos on the Pi, with size, the auto-delete time or why each is kept, and Delete now. A recording can be deleted once it's ended or turned off (`delete_asked_at`; the laptop deletes it at its next check-in). Clip videos can be deleted for a bounty's rejected or posted clips (`clips.file_deleted_at`); the clips, reviews, posts and views stay. Clip videos aren't auto-deleted, and Easton's Campaigns folders are never touched.

## D38. Content Rewards as the main marketplace (2026-11-02)
From Easton's update brief (research report, Oct 2026) and Content Rewards' own creator terms (`research/content-rewards.md`).
- **Where things live:** a Content Rewards "campaign" is a ClipForge **bounty** (it has the rate, minimum, cap and budget). The marketplace is the **campaign's** (`campaigns.marketplace`: content_rewards, the default for new ones; clipping_net; other). Old campaigns were migrated from `platform` (all Clipping.net). The campaigns list filters by marketplace and every campaign shows a badge.
- **Terms on bounties:** campaign_type (cpm, per_post, retainer, prize_pool), min_payout_usd (sets min_views = min payout ÷ rate × 1000 on save), cap_usd = max payout per clip (cap_views derived), flat_fee_bonus_usd, budget total/left and when it was checked, audience_rules, footage_scope, ai_video_banned, onscreen_ad_label_required, brief_url (the submit page), brief_text, pays_on (YouTube views vs engaged views). One form partial (`_terms.html`) everywhere.
- **Payout (`money.payout`):** 0 under the minimum; rate × views / 1000 up to the cap; plus the flat bonus; per-post pays its rate; retainers and pools don't pay per clip. Estimates are after the marketplace fee: Content Rewards takes 10% (CPM, pools; per-post and retainer under $5,000), 0% for per-post/retainer at $5,000+. Recorded payouts replace estimates.
- **Scout ranks by expected net dollars per clip:** the payout rule weighted by the odds of a clip's views. Default odds (CF_VIEW_ODDS: 50% ≥500, 30% ≥10K, 3% ≥100K, 0.3% ≥1M, nothing past CF_MAX_VIEWS=10M) until the accounts have 30 posts with views; then their own. Marked down for <$500 budget left or ≥90% used, ending in <3 weeks, a fast fill, crypto; nudged up for priority categories and sponsor segments; says when the cap cuts >25% of a breakout's value or the minimum is above the accounts' p90. Brief screenshots also fill these terms (only into empty fields).
- **Submitting by hand:** Content Rewards takes one submission per video, so the Posted tab shows a per-post checklist (copy link → open campaign page → mark submitted), then the brand's review: pending → approved (views at approval) / rejected (reason) → paid (what landed). Clipping.net keeps copy-all.
- **Before copying a caption** it must start with the disclosure and contain every required tag; otherwise "Fix caption". A moment already posted on a platform (same footage, overlapping by half; live chunks compared on the stream's timeline) can't be logged there again. A hook already used by a posted clip is flagged.
- **On-screen label** follows the bounty (`onscreen_ad_label_required`) on top of CF_ONSCREEN_DISCLOSURE; it travels in the render context (worker protocol 7).
- **Accounts** (`accounts`): platform, handle, niche; Instagram starts off. Posts record their account. Pace: 1 post a day per account through day 14, then up to CF_POSTS_PER_DAY_MAX while its last-14-day p90 holds; shown on Home and Accounts.
- **Metrics:** time logged per bounty (`work_log`), paid $/hour per bounty and week against CF_TARGET_HOURLY_USD ($50), per-account p50/p90 with bounties whose minimum is above an account's p90, YouTube engaged views (entered by hand), days from submitting to paid per marketplace.
- **Blocking:** poker, sportsbook and prediction markets (Polymarket, Kalshi) blocked by default. Political campaigns need the "brief names the sponsor and its disclosure terms" box.
- **Pings:** budget under 20% with posts not approved; a post at 70%+ of its minimum; posts on a paid bounty unsubmitted 20 h after posting. Each once.
- **Streams:** who pays for a streamer's clips (`campaigns.payer_type`); "nobody" isn't checked or recorded.
- **Layouts:** speaker-left/right, split (two speakers stacked), facecam (corner or x,y,w,h %) and vary (takes turns), per bounty or per streamer.

## D39. Tall screenshots are read in slices (2026-11-02)
- Screenshots were shrunk to 1568 px on the long edge before reading. A full-page brief (Core Boys: about 1920×5100) came out 586 px wide, and the reader got `#corebhoys` for `#coreboys`.
- Now (`stages/screenshot.py`) portrait screenshots are stored up to 1568 px **wide** at any height (`<sha>-hd.jpg`) and sent as overlapping slices top to bottom (each within the API's full-detail ~1.15 MP, 120 px overlap, at most 12, narrower if a page is taller still). The prompt says the slices are one page and to list each item once. Landscape screenshots stay one image. Scout and requirement briefs both use it.
- Cost: a tall page is ~5–8 slices (~10K input tokens, about a cent with Haiku), logged from real usage as before.
- Screenshots uploaded before this were stored shrunk; delete and re-upload one to get the sharp read.

## D40. Selection gets the brief and the names (2026-11-02)
- Six Core Boys TikToks that got paid (20K–443K views, `research/coreboys-viral.md`) all name Core Boys or a person in the on-screen text ("CORE boys react to…", "Ron CONFIRMED…", "Ishowspeed as Luffy"). Three of the six are the same moment, Speed revealed as Luffy on the Shibuya billboards, and the top one (443K) was posted within hours.
- ClipForge had that moment as a candidate but ranked it low and wrote "a famous streamer" instead of Speed. Ordinary footage never told the selector who was in it or what the brief pays for (only live chunks got names, D33). It also kept off-brief moments (the Halloween incident, spotting Adapt), and every hook said "Streamers", which breaks the brief's rule that the caption or on-screen text names Core Boys or one of them.
- Now a bounty has **Who's in the footage** (`people`) and **Caption must name one of** (`must_mention`); the brief reader fills both. Selection prompts (`select-v1`, `select-v2`, `live-v1`) get `{brief}` (footage scope, the brief's rules, the must-name list) and `{people}`, and say: skip moments the brief doesn't pay for, name people the way fans know them, and treat a famous person showing up as a top moment.
- The render step puts the first must-name name in front of a caption that lacks one; the Post tab flags a caption or on-screen text that doesn't name one (hashtags don't count), and Fix caption adds it.

## D41. YouTube channels as footage (2026-11-02)
- Some briefs allow any video from a channel. Pasting a channel link as footage used to be treated as one video (yt-dlp would walk and download the whole channel into one file), and the campaign form's "YouTube RSS feeds to watch" never worked for a channel link. YouTube's RSS feeds now return 404 for every channel tried, so that path is dead too; the field is gone from the form.
- Now each bounty has **YouTube channels** (`bounty_channels`, `clipforge/channels.py`). Adding one (or pasting a channel link into "Add links") opens a picker: the channel's last 60 uploads or past streams from yt-dlp's flat listing (no download), with length, views and an approximate date, sorted newest or most viewed. Videos under `CF_CHANNEL_MIN_VIDEO_S` (3 min) are hidden, Shorts aren't in the uploads tab, and videos ClipForge already has are greyed. Nothing runs until Easton confirms, with the same cost estimate as the folder picker.
- **Watch new uploads** per channel: every 30 min the scheduler lists the newest 20 uploads and 10 past streams. Anything not seen before that is long enough is queued when the bounty has auto-queue on, otherwise announced on Discord with a link to the picker. Turning watch on marks what's already there as seen (no backlog flood); live or unprocessed videos wait for the next poll.
- Guards: `add_source` refuses a channel link, and fetch refuses any link that turns out to be a channel or playlist.
