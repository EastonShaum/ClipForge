# Build vs Buy: AI Clipping Tools vs ClipForge's Custom Pipeline

Research date: 2026-10-26. All URLs accessed 2026-10-26 unless noted. "⚠ possibly stale" = published before 2026-04-26.

Context: ClipForge runs on a **Raspberry Pi 5 + ffmpeg**, with transcription from a **hosted Whisper API** and clip selection by **Claude**. The target is ~**5–15 posted clips/day** (150–450/month).

## TL;DR

- **Keep building. Don't replace any pipeline stage with a SaaS tool right now.** At 5–15 clips/day the custom pipeline costs about **$8–$45/month** in API fees. The cheapest workable SaaS replacements cost **$90–$400+/month**, and several plans can't even handle the volume.
- **Clip selection: keep Claude.** It costs pennies per source hour (about $0.07/hour on Sonnet 5.5), and you control the prompt: campaign rules, gambling filter, hook scoring. OpusClip/Vizard/Klap charge by **source minutes**, which is the expensive way to pay when you scan 1–3 hours of footage per day.
- **Captions: build them yourself (ffmpeg + ASS subtitles from word timestamps).** Campaign reviewers reject clips with **default, recognizable AI caption templates** and **third-party watermarks** ([FindClout, 2026-09-27](https://findclout.com/blog/ai-clip-makers-for-clippers)), so a custom style is an advantage. If you ever outsource captions, the cheapest is the **Captions/Mirage API at $0.15/min** (~$0.15 per clip).
- **Reframing / face tracking is the only stage worth a paid fallback, and only if custom quality falls short.** Most podcast and gaming layouts don't need real tracking (a fixed speaker crop, or facecam-over-gameplay stacking). The Pi 5 has **no hardware H.264 encoder** ([Raspberry Pi whitepaper](https://pip-assets.raspberrypi.com/categories/685-app-notes-guides-whitepapers/documents/RP-010033-WP-1-H.264%20encoding%20performance%20on%20Raspberry%20Pi%205_series%20computers.pdf)), so keep encodes simple. Klap's API can reframe and caption an existing short for ~$0.92/clip if needed.
- **Watch the terms:** OpusClip's **free tier is non-commercial use only**, and paid campaign clipping is commercial ([OpusClip ToS](https://www.opus.pro/terms-of-service)). Every tool's free tier adds a watermark. All of them require you to hold the rights to what you upload; a campaign brief generally gives you that, but keep the brief as proof.

---

## Tool-by-tool

| Tool | What it does | Pricing (monthly unless noted) | API? | Commercial / ToS notes | Watermark | Source |
|---|---|---|---|---|---|---|
| **OpusClip** | Long video in, AI-picked clips out; AI reframe (genre-specific model, screenshare/gameplay layouts); animated captions; auto hook; "moving object tracking" on Pro+ | Free $0 (60 min/mo); **Starter $15 (150 min)**; **Pro $29 (300 min; $174/yr annual)**; Business custom. 1 credit = 1 min of **uploaded** video. Credits can't be bought separately; processing stops at 0. | Yes, on **Pro (Beta)/"Max"/Business** only: create project, query clips, edit, publish, webhooks. **10-credit minimum per API project**; 15 h/mo of API video on Pro Beta/Max; 30 req/min; 4 parallel projects. A "Max" plan is mentioned in API docs but not on the pricing page. | **Free = "internal, personal, non-commercial use" only**; paid = commercial allowed. You warrant you have rights to uploads. You grant OpusClip a perpetual, irrevocable license to your uploads to run the service (and public submissions for marketing). | Yes on Free (animated caption templates) | [opus.pro/pricing](https://www.opus.pro/pricing); [Castmagic, verified 2026-08-20](https://www.castmagic.io/blog/opus-clip-pricing); [API docs](https://help.opus.pro/api-reference/overview); [ToS](https://www.opus.pro/terms-of-service) |
| **Vizard** | AI clipping, auto-reframe with speaker tracking, subtitles + translation, transcript export | Free (60 credits/mo, 720p export, 3-day storage). Creator / Business: credit tiers from 600/mo up to 74,400/yr; **prices didn't render on the official page**. Third parties disagree: Creator **$29** ([Capterra](https://www.capterra.com/p/10009818/Vizard/pricing/)) vs **$20** ([SendShort, 2026-01-11](https://sendshort.ai/guides/vizard-review/) ⚠). **Unverified.** | Yes, **included in paid plans**, drawing from the same minute allowance. Official page lists rate limits for all tiers (Free 1/min; Creator 3/min; Business 10/min). | ToS **not retrieved** (fetch failed). Commercial terms for the free plan unknown. | Yes on Free | [vizard.ai/pricing](https://vizard.ai/pricing); [Vizard API docs, updated 2025-04-13](https://docs.vizard.ai/docs/pricing) ⚠ possibly stale; [help: free plan](https://help.vizard.ai/en/articles/8767572-what-does-the-free-plan-for-vizard-offer) |
| **Klap** | Long video in, shorts with captions + AI reframe; can also **just caption and reframe an existing short** | Annual billing: Basic **$14** (100 clips), Pro **$39** (300 clips), Pro+ **$94** (1,000 clips). "Save 50%" yearly, so monthly is ~2× (not shown). | **Yes, usage-based:** video input **$0.44**/op, generate short **$0.32**/op, export **$0.48**/op. REST + OAuth for Zapier/Make/n8n. | ToS **not retrieved** (klap.app/terms returned 404). | Not stated | [klap.app/pricing](https://klap.app/pricing); [API docs](https://docs.klap.app); [API pricing](https://docs.klap.app/pricing) |
| **Submagic** | Captions (100+ languages, animated templates), "Magic Clips" from YouTube links, B-roll | Starter **$19** ($12/mo yearly), 15 videos ≤2 min; Pro **$39** ($23), 40 videos ≤5 min; Business **$69** ($41), 100 videos ≤30 min. | Yes: captions, Magic Clips, templates, webhooks, **MCP server**. API minutes per plan conflict on the page (10 vs 100 min/mo); add-on packs **$57/250 min**, then $0.15/min down to $0.10/min at volume. Reframe not mentioned. | ToS **not retrieved** (404). | "No watermark" on all paid plans | [submagic.co/pricing](https://www.submagic.co/pricing); [API docs](https://docs.submagic.co) |
| **Captions (now Mirage)** | Consumer editing app; the **API** offers styled captioning (and avatar video generation) | API: **Add Captions $0.15/min of input, rounded up per video** (a 45s clip = $0.15). App: from $9.99/mo for 200 credits (iOS pricing). | Yes (captions, avatar generation). **No clip selection or reframe in the API pricing.** | ToS not reviewed. | n/a for API | [API pricing](https://captions.ai/help/docs/api/pricing); [hooked.so review](https://www.hooked.so/compare/captions-ai-pricing) |
| *Similar:* Eklipse, StreamLadder | Stream/gaming-focused auto clipping (Twitch/Kick VODs) | Free tiers; paid prices not confirmed | Not researched | — | Free-tier watermarks | [FindClout, 2026-09-27](https://findclout.com/blog/ai-clip-makers-for-clippers) |

### Output quality

I found **no independent, controlled quality benchmark** for these tools. Every comparison I found was written by a competitor or vendor. The most useful practical finding is that **no tool reliably produces campaign-approved clips without a human pass.** The common rejection reasons are default caption styling, a hook that doesn't land in the first second (AI tends to pick good *middles*, not openers), missing context, third-party watermarks, and off-brief topics ([FindClout, 2026-09-27](https://findclout.com/blog/ai-clip-makers-for-clippers), a competitor-written guide). OpusClip's higher tiers add visual/emotion detection, which helps it spot good moments (same source). **Quality claims beyond that are unverified.** The only reliable way to compare is to run the same 2–3 source videos through OpusClip Starter ($15, 1 month) and ClipForge side by side.

---

## Cost comparison at 5–15 clips/day

**Assumptions (mine, so adjust them):** 150–450 posted clips/month; ~45-second clips; you scan ~1 source hour per 5 clips you post, so **30–90 source hours/month = 1,800–5,400 source minutes**; transcripts are ~20K input tokens per source hour, plus ~3K output tokens of clip picks.

**Price inputs:**
- Transcription: gpt-4o-mini-transcribe **$0.003/min**, whisper-1 / gpt-4o-transcribe **$0.006/min** (aggregator sources; check OpenAI's pricing page; one source says a new `gpt-transcribe` costs $0.0045/min and gpt-4o-mini-transcribe retires 2027-02-26) ([diyai.io](https://diyai.io/ai-tools/speech-to-text/openai-whisper-api-pricing-2026/); [developersdigest, 2026-10-18](https://www.developersdigest.tech/models/speech/openai)).
- Claude (Anthropic first-party, per Anthropic's model table cached 2026-10-06): **Sonnet 5.5 $2/$10 per M tokens in/out; Opus 5.5 $4/$20; Haiku 5.5 $0.10/$0.50** ([Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)).

### Custom (current plan)

| Stage | Monthly cost (low → high volume) |
|---|---|
| Transcription (gpt-4o-mini-transcribe / whisper-1) | $5.40 → $16.20 / $10.80 → $32.40 |
| Clip selection, Claude Sonnet 5.5 (~$0.07 per source hour) | $2.10 → $6.30 |
| Clip selection, Claude Opus 5.5 instead (~$0.14 per source hour) | $4.20 → $12.60 |
| Captions (ffmpeg + ASS from word timestamps) | $0 |
| Reframe (fixed crops / MediaPipe face detection on Pi) | $0 (Pi CPU time only) |
| **Total** | **≈ $8 → $45/month** |

### Buy options (doing the same stage)

| Replace… | With | Monthly cost at 150 → 450 clips | Works at this volume? |
|---|---|---|---|
| Selection + captions + reframe | **OpusClip Pro** ($29 for 300 source min) | Needs 1,800–5,400 min; Pro covers only **300**. Would need Business (custom pricing). At Pro's rate (~$0.097/min) it would be **~$174 → $522**, but extra credits can't be bought. | **No** (on self-serve plans) |
| Selection + captions + reframe | **Vizard** Creator/Business (credit-based) | At the $29 / 600-min base (unverified price), ≈ $0.048/min → **~$87 → $260** for 1,800–5,400 min, if higher tiers scale linearly (unknown) | Maybe; pricing unverified |
| Selection + captions + reframe | **Klap subscription** | Pro $39 (300 clips) → Pro+ $94 (1,000 clips), annual billing. You'll *generate* 2–3× more clips than you post, so expect Pro+. **~$94** (≈$188 if billed monthly, inferred from "Save 50%") | Yes, but no Pi integration without the API |
| Selection + captions + reframe | **Klap API** | Per source video $0.44 + per short $0.32 + per export $0.48. 30–90 sources/mo, generating 2× the posted count (300–900 shorts, all exported): **≈ $250 → $760** | Yes; expensive |
| Captions only | **Captions/Mirage API** ($0.15 per clip ≤1 min) | **$22.50 → $67.50** | Yes; cheapest outsourcing option |
| Captions only | **Submagic** Business + API pack | 113–338 clip-minutes: **~$69 → $126** | Yes |
| Reframe + captions on finished cuts | **Klap API** (input $0.44 + export $0.48) | **~$138 → $414** | Yes |

**Verdict:** buying costs **3–15× more** than the whole custom pipeline, and it **reduces** approval odds (template captions, less control over hooks and context).

---

## Stage-by-stage recommendation

| Stage | Recommendation | Why |
|---|---|---|
| **Selection** | **Build (Claude).** Start on Sonnet 5.5; A/B against Opus 5.5 on hook quality (+~$6/month at most). | ~$0.07 per source hour vs ~$5.80/hour of source on OpusClip Pro. Claude can also read the campaign brief (required tags, banned topics, gambling filter), which no SaaS tool does. |
| **Captioning** | **Build (ffmpeg `subtitles`/ASS burn-in from word timestamps).** Fallback: Captions/Mirage API at $0.15/clip. | Custom styles avoid the "default template" rejection. Check that your Whisper model returns **word-level timestamps**: I could not verify this for gpt-4o-mini-transcribe, and it's worth testing before relying on it. |
| **Reframe / face tracking** | **Build simple first:** podcasts use a fixed per-speaker crop (switch on the speaker from transcript diarization or a simple face-position check); gaming uses a facecam-top / gameplay-bottom stack (no tracking). Only if quality fails, run hard cases through **Klap's API** (~$0.92/clip) or try OpusClip Starter by hand. | MediaPipe runs on a Pi 5, but I found **no published FPS benchmark** for it, so benchmark it yourself (e.g. detect faces at 2–5 fps and smooth the crop, rather than every frame). |
| **Encoding** | Keep libx264 at a fast preset; avoid 4K sources; render 1080×1920 at 30fps. | The Pi 5 has no hardware H.264 encoder. Raspberry Pi says real-time 1080p30 libx264 is feasible in low-latency mode on ~60% of one core; higher-quality modes cost more CPU ([whitepaper](https://pip-assets.raspberrypi.com/categories/685-app-notes-guides-whitepapers/documents/RP-010033-WP-1-H.264%20encoding%20performance%20on%20Raspberry%20Pi%205_series%20computers.pdf), undated). At 15 clips × 45s/day this is well within budget. |

**One cheap experiment worth doing:** pay for **OpusClip Starter for one month ($15)** and run the same 3 sources through both tools. Compare which clips Claude picks with which OpusClip picks, and the reframe quality on multi-speaker podcasts. This gives you a quality benchmark for ClipForge. Don't post OpusClip free-tier output (non-commercial and watermarked).

## Open questions for Easton

1. Roughly how many source hours do you expect to scan per day? The cost math scales with source minutes, not with clips posted.
2. Which transcription model is in use now, and does it return word-level timestamps? (This decides how easy custom captions are.)
3. Do the target gaming campaigns supply a facecam + gameplay layout, or a single combined feed? (This decides whether reframing needs tracking at all.)
4. OK to spend $15 on a one-month OpusClip Starter benchmark?
