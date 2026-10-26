# ClipForge

Turns long-form campaign footage (podcasts, interviews, gaming streams) into ready-to-post vertical clips, then hands them to a human for review.

```
campaign + footage link → download → transcribe (Whisper) → AI picks moments (Claude)
→ render 9:16 with word-by-word captions → Discord ping → approve on phone
→ copy caption, post by hand → log URL & views → dashboard + feedback to the AI
```

It runs on a Raspberry Pi 5, with an optional Windows laptop worker over Tailscale. Posting is always manual.

## Quick start (Pi)
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # once
cp .env.example .env                                # add keys (see BLOCKERS.md)
scripts/install-pi.sh                               # deps, DB, systemd services
```
Open `http://<pi-tailscale-name>:8420`, add a campaign, and paste a footage link.

## Try it offline
```bash
CF_TRANSCRIBE_PROVIDER=fake CF_SELECT_PROVIDER=fake uv run pytest -q
uv run clipforge bench          # render speed on this machine
```

## Docs
- [docs/architecture.md](docs/architecture.md): diagram, data flow, code map
- [docs/runbook.md](docs/runbook.md): operate, back up, troubleshoot
- [docs/launch-playbook.md](docs/launch-playbook.md): warm-up, daily routine, kill criteria
- [research/SUMMARY.md](research/SUMMARY.md): what the research says, where to start
- [DECISIONS.md](DECISIONS.md): why it's built this way
