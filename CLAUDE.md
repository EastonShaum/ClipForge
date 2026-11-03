# ClipForge: project memory

A semi-automated clipping pipeline for paid view-based campaigns (Clipping.net, later Whop). The machine finds moments in campaign-supplied footage and renders captioned 9:16 clips. Easton reviews them on his phone, posts manually, and logs URLs and views. Owner: Easton (full-stack dev, beginner clipper, Utah).

## Hard constraints (never violate)
- No Discord self-bots. Discord is a **webhook** for notifications only. Campaign ingest is manual or via RSS.
- No automated posting to any social platform. The one exception is D32: ClipForge may upload a draft to Easton's TikTok inbox when he taps the button, and he publishes it himself.
- Only clip footage a campaign supplies or permits. Every source belongs to a campaign.
- No scraping behind Content Rewards' or Clipping.net's login or bot checks. Campaign info comes from Scout screenshots or manual entry; CR has no public API (checked 2026-11-02).
- FTC disclosure on every clip: `#ad` first in the post caption, and the platform's paid-partnership toggle on every post. The burned-in "Paid partnership #ad" label is off by Easton's call (D31, `CF_ONSCREEN_DISCLOSURE`).
- Gambling, casino, sportsbook, poker and prediction-market campaigns blocked by default (`CF_BLOCKED_*`). Political ones need the brief to name the sponsor and disclosure terms.
- Secrets only in `.env` (gitignored). `.env.example` is the template.
- Every paid API call is logged in `api_costs`. The monthly cap defers paid jobs.

## Layout
- `clipforge/`: Python package. See `docs/architecture.md` for the code map and diagram.
- `tests/`: pytest. `tests/fixtures/sample.mp4` is a self-made 40 s test pattern.
- `research/`: sourced research. Start with `research/SUMMARY.md`.
- `docs/`: architecture, runbook, launch playbook.
- `deploy/systemd/`, `scripts/`: Pi services and install/backup scripts; `scripts/laptop/` is the Windows worker.
- `DECISIONS.md` (the why), `BLOCKERS.md` (needs Easton), `QUESTIONS.md` (non-blocking), `PLAN.md` (status).

## Commands
```bash
export PATH=$HOME/.local/bin:$PATH
uv sync                       # deps (Python via uv; .venv/)
uv run pytest -q              # tests, offline (fake providers), ~1 min
uv run ruff check .           # lint
uv run clipforge --help       # CLI: init, campaign add/list, source, status, serve, worker,
                              #      remote-worker, scheduler, cleanup, bench
systemctl --user status 'clipforge-*'
```

## Conventions
- **Stage handlers** (`clipforge/stages/*`) never touch the DB. `pipeline.build_context` / `apply_result` do, so the same handler runs on the Pi or the laptop.
- **Everything must be idempotent.** Use dedupe keys, `ON CONFLICT DO NOTHING`, and atomic file renames.
- Use **`jobqueue.defer`** (no attempt burned) for conditions that time fixes (budget, disk, missing key). Use `fail` for real errors.
- Times are integer ms in the DB and pipeline. LLM I/O uses seconds. All timestamps are UTC ISO strings.
- **Settings** come only from `get_settings()`. Tests set `CF_*` env vars in `tests/conftest.py`.
- **Prompts** live in `clipforge/prompts/select-*.md`. Editing one creates a new version (content hash). Template vars: `{category} {title} {n} {min_s} {max_s} {rubric} {feedback} {hook_types} {transcript}`.
- **Commits:** conventional (`feat:`, `fix:`, `docs:`, `chore:`), small.

## Current status (2026-11-02)
- Phases 0–7 are built, plus: in-place local footage pinned to the right machine (D20), laptop self-update (D21), Discord posting reminders (D22), several campaign logos as a row under the captions (D23), Scout, which ranks bounties from uploaded screenshots (D24), requirement screenshots read per campaign (D25), and one page per campaign with footage picked from the laptop's folders after a confirm step (D26), and campaigns that hold bounties, with folders `Campaigns/<campaign>/<bounty>` (D27), footage-ready pings, auto-queue and clips per video (D28), review/post/views as tabs inside each bounty (D29), and live streams: watch Kick/Twitch channels, record on the laptop (`recorder.py`), clip 30-min `.ffconcat` chunks, hold renders until the rules are in (D30), and a hook card with color emojis for the whole clip instead of burned-in captions (D31, worker protocol 4), and TikTok inbox drafts from the Post page (D32, `clipforge/tiktok.py`). Live chunks are 5 min with their own prompt pool (`prompts/live-*.md`), 6–25 s clips, guest names and a full-screen crop (D33, worker protocol 5). Lives have their own tab: each stream has its own bounty row and can be moved into a paid one, gambling-titled streams aren't clipped, and live clips pick crop or full width per clip (D34, worker protocol 6), with a per-streamer override for games (D36). Each live stream can be turned off, Lives has Streams/Streamers tabs, times show in Mountain time, and a Files page shows every saved file with its auto-delete time and a delete button (D37). The UI is a light "thumb zone" design with a bottom tab bar, a "Needs you" home, one-clip-at-a-time review, payout progress and stream timelines (D35). Content Rewards is the main marketplace (D38): terms on bounties, payout after fees, Scout ranks by expected $ per clip, a per-clip submit checklist and the brand's review status, accounts and posting pace, $/hour against $50, budget/near-minimum/submit pings, payer type per streamer, speaker/split/facecam/vary layouts (worker protocol 7). Requirement screenshots can be added with a new campaign or bounty, and finished campaigns can be archived. Tall screenshots are read in slices (D39). Selection gets each bounty's brief, who's in the footage and the names a caption must mention (D40). A bounty can list YouTube channels: pick videos from a channel's list, or watch it for new uploads (D41). Bounties can be archived on their own, keeping the campaign open (D42). "Save video" opens the share sheet (Save Video → Photos) over the https UI (D43). 133 tests pass.
- No burned-in captions (TikTok auto-captions). The hook card (`stages/hookcard.py`) is off by default (`CF_HOOK_CARD`): Easton adds the hook in TikTok on his iPhone, copied from the Post page's "Copy on-screen text" button.
- Render benchmark on the Pi: 45 s clip in 54 s (blurfill), so the Pi renders by default (D6).
- The services run on the Pi, and the UI is bound to the Tailscale IP 100.x.y.z:8420. The laptop worker (`eastons-laptop`) renders Man On.
- First real run done: Man On ep-4 produced 6 clips for $0.03. Easton is reviewing and starts posting on 2026-10-28.
- **Remote worker protocol** (`worker.WORKER_PROTOCOL`): bump it when a job context gains something a remote worker must understand. The laptop self-updates to the Pi's running commit (D21), so **deploy = commit + restart the services**. The manual zip is only for a first install: `git archive --format=zip --prefix=CLipping/ -o ~/clipforge-laptop.zip HEAD`.

## Next steps
See PLAN.md "Next". Up now: tune `select-v2.md` from Easton's reject reasons. Then the GFuel mention-based flow, which needs one streamer tab of the mentions sheet from Easton.
