# Plan

Status as of 2026-10-26. Effort is wall-clock working time.

| Phase | Milestones | Est. | Status |
|---|---|---|---|
| 0. Clarify & plan | Questions answered, PLAN.md | 0.5 h | ✅ done |
| 1. Research | 7 research files + SUMMARY.md, sourced and dated | 3 h (parallel agents) | ✅ done |
| 2. Architecture & scaffolding | uv project, schema, job queue, config, docs/architecture.md | 2 h | ✅ done |
| 3. Pipeline MVP | campaign registry, ingest + RSS, fetch, transcribe (Groq/local/fake), select (API/CLI/fake), render (blurfill/crop, ASS captions, loudnorm, disclosure), review UI, post logging, manual views, dashboard | 6 h | ✅ done; real-provider runs blocked on keys (BLOCKERS B2/B3) |
| 4. Feedback loop | few-shot feedback from reviews + views; prompt versioning; weighted A/B; dashboard per prompt version | 1.5 h | ✅ done |
| 5. Testing & hardening | 34 tests incl. end-to-end on a committed fixture; retries/backoff; budget/disk/key deferrals; health endpoint; idempotency test | 2 h | ✅ done |
| 6. Deployment | systemd user units, install script, DB backup script, laptop scripts, runbook | 1.5 h | ✅ units installed; reboot test pending (B8) |
| 7. Launch playbook | docs/launch-playbook.md with warm-up, routine, kill criteria | 1 h | ✅ done |

## Next (after blockers clear)
1. **First real run:** one real campaign plus one real source, using real keys. Review the selector's picks and tune the prompts. ~1 h with you.
2. **Laptop worker live:** run setup on Windows and confirm fetch and render offload. ~0.5 h.
3. **Automatic YouTube view tracking** via the Data API (free key). ~2 h.
4. ✅ **Layouts** (D38): speaker crop, two-speaker split, facecam over gameplay, vary.
5. **Face-tracking crop** (stretch): OpenCV on the laptop worker. ~1–2 days.
6. **Batch API** for selection at 50% off. Only worth it at more than 10 sources/day.
