# ClipForge: project memory

A semi-automated clipping pipeline for paid view-based campaigns (Clipping.net, later Whop). The machine finds moments in campaign-supplied footage and renders captioned 9:16 clips. Easton reviews them on his phone, posts manually, and logs URLs and views. Owner: Easton (full-stack dev, beginner clipper, Utah).

## Hard constraints (never violate)
- No Discord self-bots. Discord is a **webhook** for notifications only. Campaign ingest is manual or via RSS.
- No automated posting to any social platform.
- Only clip footage a campaign supplies or permits. Every source belongs to a campaign.
- FTC disclosure on every clip: `#ad` first in the caption, plus an on-screen "Paid partnership #ad".
- Gambling/casino campaigns blocked by default (`CF_BLOCKED_*`).
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

## Current status (2026-10-27)
- Phases 0–7 are built, plus: in-place local footage pinned to the right machine (D20), laptop self-update (D21), Discord posting reminders (D22), several campaign logos as a row under the captions (D23, worker protocol 3), and Scout, which ranks bounties from uploaded screenshots (D24). 65 tests pass.
- Captions are one fixed line, split by measured pixel width (render.py `_GLYPH_W`).
- Render benchmark on the Pi: 45 s clip in 54 s (blurfill), so the Pi renders by default (D6).
- The services run on the Pi, and the UI is bound to the Tailscale IP 100.x.y.z:8420. The laptop worker (`eastons-laptop`) renders Man On.
- First real run done: Man On ep-4 produced 6 clips for $0.03. Easton is reviewing and starts posting on 2026-10-28.
- **Remote worker protocol** (`worker.WORKER_PROTOCOL`): bump it when a job context gains something a remote worker must understand. The laptop self-updates to the Pi's running commit (D21), so **deploy = commit + restart the services**. The manual zip is only for a first install: `git archive --format=zip --prefix=CLipping/ -o ~/clipforge-laptop.zip HEAD`.

## Next steps
See PLAN.md "Next". Up now: tune `select-v2.md` from Easton's reject reasons. Then the GFuel mention-based flow, which needs one streamer tab of the mentions sheet from Easton.
