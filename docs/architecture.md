# Architecture

```
                         ┌──────────────── You (phone, over Tailscale) ────────────────┐
                         │  Review · Post · Views · Sources · Campaigns · Dashboard     │
                         └───────────────▲──────────────────────────────┬──────────────┘
                                         │ HTTP (tailnet only)          │ Discord ping
┌────────────────────── Raspberry Pi 5 (always on) ───────────────────────┼──────────────┐
│                                        │                              │              │
│  clipforge-web (FastAPI) ──────────────┴── /api/worker/* ◄─────────────┼─── pull ──┐  │
│        │  reads/writes                                                │           │  │
│        ▼                                                              │           │  │
│  ┌──────────── SQLite (WAL) ─────────────┐     clipforge-scheduler ───┘           │  │
│  │ campaigns sources transcripts         │      • YouTube RSS polling             │  │
│  │ candidates clips reviews posts        │      • raw-media cleanup               │  │
│  │ view_snapshots api_costs              │      • budget alerts                   │  │
│  │ prompt_versions jobs workers          │                                        │  │
│  └──────▲───────────────────▲────────────┘                                        │  │
│         │ claim/complete    │                                                     │  │
│  clipforge-worker-light     clipforge-worker-render                               │  │
│   fetch → transcribe →       render (ffmpeg, libass)                              │  │
│   select                                                                          │  │
│     │        │       │                                                            │  │
└─────┼────────┼───────┼────────────────────────────────────────────────────────────┼──┘
      │        │       │                                                            │
      ▼        ▼       ▼                                                            │
  yt-dlp   Groq      Anthropic API                    ┌──── HP laptop (optional) ───┴──┐
 (YouTube) Whisper   Claude Haiku 5.5                 │ clipforge remote-worker        │
           (words +  (scored candidates,              │  fetch / transcribe / render   │
           timings)   strict JSON)                    │  raw media on 758 GB disk      │
                                                      │  uploads finished clips only   │
                                                      └────────────────────────────────┘
```

## Data flow for one source
1. **Ingest:** a link (UI, CLI or RSS) becomes a `sources` row keyed by `source_key` (e.g. `youtube:<id>`), plus a `fetch:<sid>` job.
2. **fetch:** yt-dlp downloads at ≤1080p into `data/raw/<sid>/`. It checks free disk and the raw-media cap first. It records `media_host` (pi or laptop), then queues `transcribe:<sid>` pinned to that host.
3. **transcribe:** extracts 16 kHz mono Opus and sends 10-minute chunks to Groq. Word timestamps go into `transcripts`. The cost is logged. Queues `select:<sid>` pinned to the Pi.
4. **select:** picks a prompt version (weighted A/B) and builds the prompt: rubric, feedback examples, compact transcript. Calls Claude and gets JSON back. Times are snapped to word boundaries, overlaps dropped, and the result stored in `candidates`. The top N (`CF_CANDIDATES_KEEP`) get `render:<cid>` jobs pinned to the media host.
5. **render:** ffmpeg cuts the clip and builds the 9:16 blurfill frame with ASS captions (hook, word-by-word captions, disclosure tag), then loudnorm at −14 LUFS. Output goes to `data/clips/<sid>/c<cid>.mp4`. The caption text is built and the clip lands `in_review`. When the last render for a source finishes, Discord gets "N clips ready".
6. **Review → post → views:** you approve or reject (with a reason), post by hand, paste the URL back, and enter views. All of it feeds the dashboard and the next prompt's feedback block.

## Code map
| Path | What it does |
|---|---|
| `clipforge/config.py` | All settings (`CF_*` env vars / `.env`) |
| `clipforge/schema.sql`, `db.py` | Schema, connections, `tx()` = `BEGIN IMMEDIATE` |
| `clipforge/jobqueue.py` | enqueue / claim (leases, affinity) / complete / fail / defer |
| `clipforge/pipeline.py` | `build_context` → `run_handler` → `apply_result`, shared by local and remote workers |
| `clipforge/stages/*.py` | fetch, transcribe, select, render: the actual work, no DB access |
| `clipforge/worker.py` | Local loop (Pi) and remote HTTP loop (laptop) |
| `clipforge/web/` | FastAPI app, Jinja templates, CSS |
| `clipforge/feedback.py`, `select_prompts.py`, `prompts/` | Feedback loop, prompt versioning, prompt text |
| `clipforge/analytics.py`, `costs.py`, `campaigns.py`, `captions.py`, `ingest.py`, `notify.py`, `scheduler.py` | As named |
| `clipforge/live.py` | Pi side of live streams: who's live, sessions, chunks → sources, recorder plan (D30) |
| `clipforge/recorder.py` | Laptop side: records streams as `.ts` pieces, writes overlapping `.ffconcat` chunks |
| `clipforge/bounties.py` | Bounties, plus render holds (`hold_reasons`, `release`) |

## Job and entity states
```
jobs:     queued ──claim──► running ──ok──► done
             ▲                 │
             └── fail (backoff)┤──► dead (after max_attempts)
             └── defer (budget/disk/key; no attempt used)

sources:  new → fetched → transcribed → selected → rendered      (or failed)
clips:    in_review → approved → posted                          (or rejected)
```
