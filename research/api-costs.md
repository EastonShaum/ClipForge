# API Costs: transcription + Claude for ClipForge

Researched 2026-10-26. All sources accessed 2026-10-26. Anything published or last confirmed before 2026-04-26 is marked **⚠ possibly stale**. Prices are USD.

## TL;DR

- **Transcription: use Groq `whisper-large-v3-turbo`.** It costs $0.04 per audio hour on paid usage. The free tier needs no credit card and allows 28,800 audio seconds a day (8 h/day) and 7,200 audio seconds per hour, with 25 MB per upload on free (100 MB on Developer). It returns word timestamps via `verbose_json` plus `timestamp_granularities=["word"]`. 2–6 h/day fits inside the free daily cap, and costs about $2.40–$7.20/month if you upgrade. [T1][T2][T3]
- **Fitting the size limit:** convert to 16 kHz mono Opus at 24 kbps (about 10.8 MB per hour, so 1 h fits in one free-tier request). Chunk into roughly 20-min pieces with 3–5 s overlap anyway, to stay under the 2 h-per-hour rate limit and to retry cheaply. [T3]
- **Local fallback (CPU-only i5/i7 laptop, int8):** estimates only. `small` runs about 2–3× real time (1 h of audio in 20–30 min). `large-v3-turbo` runs about 1–1.5× (40–60 min). `medium` runs about 0.8–1.2× (50–75 min). It works for overnight jobs but is slower and fiddlier than Groq. [T9][T10]
- **Claude cost per 1-hour transcript** (~22K input / 4K output tokens, segment-level timestamps): **Haiku 5.5 ≈ $0.004, Sonnet 5.5 ≈ $0.084, Opus 5.5 ≈ $0.168.** At 10 sources a day that is about $1.26 / $25 / $50 a month. The Batch API halves these. Prices verified on Anthropic's pricing page. [C1]
- **`claude -p` on a Pro plan:** headless `-p` is a documented Claude Code feature, and signing in to the unmodified CLI with your own subscription is explicitly allowed. Pro limits (a 5-hour session limit plus a weekly limit) are shared with claude.ai chat. Anthropic says advertised limits "assume ordinary, individual usage". The Consumer Terms ban "automated or non-human means" except via API key "or where we otherwise explicitly permit it". **No official statement explicitly allows or forbids an unattended cron pipeline on Pro.** For a scheduled pipeline, the API key is the clearly sanctioned route. [D1–D5]

---

## (a) Transcription providers with word-level timestamps

| Provider / model | Price per audio hour | Free tier | Max file per request | Word timestamps | Signup friction | Source |
|---|---|---|---|---|---|---|
| **Groq `whisper-large-v3-turbo`** | **$0.04** | No card needed. 20 RPM, 2K req/day, **7,200 audio-s/hour, 28,800 audio-s/day** (see note) | **25 MB free / 100 MB Developer**. Use the `url` param for larger files. Billed min 10 s per request. | Yes: `response_format=verbose_json`, `timestamp_granularities=["word"]` (word + start/end) | Low: email/Google/GitHub login, API key | [T1][T2][T3][T4] |
| Groq `whisper-large-v3` | $0.111 | Same limits | Same | Yes | Low | [T2][T1] |
| OpenAI `whisper-1` | $0.36 ($0.006/min) | None (prepaid credit) | 25 MB | **Yes**. OpenAI says `timestamp_granularities[]` "is only supported for `whisper-1`". **Deprecated, shutdown 2027-02-26.** | Medium: card + prepaid credits | [T5][T6] |
| OpenAI `gpt-4o-transcribe` | $0.36 | None | 25 MB | **No word timestamps documented** (only whisper-1 supports them). Deprecated, shutdown 2027-02-26. | Medium | [T5][T6] |
| OpenAI `gpt-4o-mini-transcribe` | $0.18 | None | 25 MB | No (same) | Medium | [T5][T6] |
| OpenAI `gpt-transcribe` (new recommended model) | $0.27 ($0.0045/min) | None | 25 MB | Not documented. The API reference only shows timestamp examples with whisper-1. | Medium | [T5][T6][T7] |
| Deepgram Nova-3 (pre-recorded) | $0.258 mono ($0.0043/min). $0.312 multilingual. | **$200 credit, no card required, doesn't expire** | 2 GB. 10-min processing timeout. | Yes (word objects with timings) | Low | [T8][T11] |
| Deepgram Whisper Large | $0.288 ($0.0048/min) | Same credit | 2 GB. Whisper concurrency 5. | Yes | Low | [T8] |
| AssemblyAI Universal-2 | $0.15 | **$50 credit, no card** | 2.2 GB local upload. 5 GB / 10 h via `audio_url`. | Yes: `start`/`end` in ms, ±~400 ms | Low | [T12][T13][T14] |
| AssemblyAI Universal-3.5 Pro | $0.21 | Same credit | Same | Yes | Low | [T12] |
| DeepInfra `whisper-large-v3-turbo` (cheaper option) | **$0.012** ($0.0002/min) | No free credit mentioned | Not stated | Yes ("word" chunk level) | Medium: account + card | [T15] |

**Notes**
- **Groq limits table ambiguity:** Groq's rate-limits page lists whisper limits of RPM 20 / RPD 2K / ASH 7.2K / ASD 28.8K. It then calls the table "the base limits for the Developer plan", while also telling users to "upgrade to Developer plan… to access higher limits". Several third-party 2026 guides quote the same numbers as the **free** tier. Check `console.groq.com/settings/limits` after signup. [T1][T4]
- **Groq file size:** the speech-to-text guide says 25 MB on free and 100 MB on dev, and that Groq downsamples to 16 kHz mono internally. [T3]
- **OpenAI is not a fit:** the only OpenAI model with word timestamps (`whisper-1`) is deprecated and costs 9× Groq turbo. [T5][T6]
- **DeepInfra** is about 3× cheaper than Groq, but at 2–6 h/day the difference is under $5/month and Groq's free tier costs $0. DeepInfra price comes from its model page. Some third-party trackers also list OpenRouter at about $0.011/h. [T15]
- **Deepgram's $200 / AssemblyAI's $50 credits** cover roughly 775 h / 333 h of audio respectively. Both are useful as a backup provider.

### Recommendation for a beginner doing 2–6 h/day

1. **Primary: Groq free tier, `whisper-large-v3-turbo`.** 6 h/day = 21,600 audio-s, under the 28,800 ASD cap. The hourly cap (7,200 audio-s/h = 2 h of audio per wall-clock hour) means a 6 h backlog needs at least 3 h to submit. Space requests with a queue and back off on HTTP 429. If you outgrow free: 180 h/month × $0.04 = **$7.20/month**.
2. **Backup:** Deepgram Nova-3 on the $200 free credit (no card), with the same word-level output.
3. **Offline fallback:** local faster-whisper on the Windows laptop (section b).

### File-size workaround (do this for every provider)

```bash
# Extract audio: 16 kHz, mono, Opus 24 kbps in an Ogg container
# (Groq accepts ogg; ~10.8 MB per hour)
ffmpeg -i input.mp4 -vn -ac 1 -ar 16000 -c:a libopus -b:a 24k -application voip audio.ogg

# Alternative: MP3 32 kbps (~14.4 MB/hour) if a provider rejects ogg
ffmpeg -i input.mp4 -vn -ac 1 -ar 16000 -c:a libmp3lame -b:a 32k audio.mp3
```

Size per hour at a given bitrate = kbps × 450 KB (e.g. 24 kbps ≈ 10.8 MB, 48 kbps ≈ 21.6 MB). A 16 kHz 16-bit mono WAV is about 115 MB/h, so never upload WAV. Groq resamples to 16 kHz mono anyway, so a higher sample rate adds nothing. [T3] The bitrate figures are arithmetic, not sourced.

**Chunking with overlap** (Groq's docs recommend overlapping segments and merging [T3]):
- Split into about 20-min chunks (about 3.6 MB each at 24 kbps) with **3–5 s overlap**. Prefer cutting at a silence found with `ffmpeg -af silencedetect=n=-35dB:d=0.5`.
- Send each chunk and add the chunk's start offset to every word timestamp.
- Merge: pick a cut point in the middle of each overlap. Keep words from chunk N with `start < cut` and words from chunk N+1 with `start >= cut`. This drops duplicate words.
- Benefits: stays far under 25 MB, a failed chunk is retried alone, and you can pace the 7,200 audio-s/h limit.

---

## (b) Local faster-whisper on a CPU-only Windows 10 laptop

**Measured reference point** (faster-whisper README): `small`, int8, beam 5, 13 min of audio on an **Intel i7-12700K with 8 threads**: **1 min 42 s** (≈7.6× real time, 1.5 GB RAM). Batched (`batch_size=8`): 51 s (≈15×, 3.6 GB RAM). The README has no CPU numbers for medium, large-v3 or turbo. [T9]

**Other data:** one GitHub commenter (hardware unknown) reported a 10-min clip at 52 s with `medium` vs 39 s with `turbo`, i.e. turbo about 25% faster than medium. ⚠ possibly stale (Oct 2024). [T10]

**Estimates for a typical 4-core / 8-thread mobile i5/i7 (8th–12th gen), int8, beam 5, `word_timestamps=True`.** These are scaled down about 2.5–3.5× from the 12700K desktop number. They are *estimates, not measurements*:

| Model | Approx speed | 1 h of audio takes | RAM | Accuracy note |
|---|---|---|---|---|
| `small` int8 | ~2–3× real time | ~20–30 min | ~1.5 GB | Noticeably more errors on names, slang, gaming jargon |
| `medium` int8 | ~0.8–1.2× | ~50–75 min | ~2.5–3 GB | Good |
| `large-v3-turbo` int8 | ~1–1.5× | ~40–60 min | ~2–3 GB | Close to large-v3. **The best local choice.** |
| `large-v3` int8 | ~0.3–0.5× | ~2–3 h | ~3–4 GB | Best, but too slow on CPU |

Tips: use `BatchedInferencePipeline` (README shows about 2× speedup on CPU, with VAD on by default), set `cpu_threads` to the physical core count, and plug in the laptop with the "Best performance" power mode. Benchmark a 5-minute file before trusting the table. [T9]

**Verdict:** at 2–6 h/day, turbo on CPU needs about 2–9 h of laptop compute a day. That's workable overnight, but Groq free does it in minutes for $0. Keep local only as an offline fallback.

---

## (c) Claude API cost for clip selection

### Verified prices (platform.claude.com pricing page, accessed 2026-10-26) [C1]

| Model | Input / MTok | Output / MTok | Cache hit / MTok | Batch (50% off) in / out |
|---|---|---|---|---|
| Claude Haiku 5.5 (prompts ≤100K tokens) | $0.10 | $0.50 | $0.01 | $0.05 / $0.25 |
| Claude Haiku 5.5 (prompts >100K tokens) | $0.50 | $2.50 | $0.05 | — |
| Claude Sonnet 5.5 | $2 | $10 | $0.10 | $1 / $5 |
| Claude Opus 5.5 | $4 | $20 | $0.20 | $2 / $10 |

The prices you supplied ($0.10/$0.50, $2/$10, $4/$20) **match** the official page. Two caveats from the same page: **Haiku 5.5 is priced by prompt length**, and any request over 100,000 input tokens pays 5× ($0.50/$2.50). US-only inference (`inference_geo`) adds a 1.1× multiplier. Batch API is 50% off input and output. [C1]

### Token estimate for a 1-hour source

- ~10K words of speech ≈ ~13K tokens of plain text (rule of thumb of about 1.3 tokens per word, an estimate; check with the token-counting endpoint).
- **Compact encoding (recommended):** send **segment-level timestamps**, not per-word. Use one line per ~10-word phrase, e.g. `[734.2] so I'm sitting in the police car right`. That is about 1,000 lines × ~5 extra tokens = ~5K tokens, giving a **transcript of about 18K tokens**. Keep per-word timestamps locally and snap the LLM's start/end to word boundaries in code.
- Rubric and system prompt: about 3K. Overhead: about 1K. **Total input ≈ 22K tokens.** Output: about 4K (15–20 JSON candidates).
- (Per-word timestamps inline would be about 75–80K input tokens, about 3.5× the cost, and push 1.5 h+ sources over Haiku's 100K threshold.)

### Cost per source (1 h, 22K in / 4K out, standard pricing)

| Model | Input cost | Output cost | **Per 1-h source** |
|---|---|---|---|
| Haiku 5.5 | 22K × $0.10/M = $0.0022 | 4K × $0.50/M = $0.0020 | **$0.0042** |
| Sonnet 5.5 | 22K × $2/M = $0.044 | 4K × $10/M = $0.040 | **$0.084** |
| Opus 5.5 | 22K × $4/M = $0.088 | 4K × $20/M = $0.080 | **$0.168** |

### Monthly cost (30 days, 1-h sources)

| Model | 2 sources/day (60/mo) | 5 sources/day (150/mo) | 10 sources/day (300/mo) |
|---|---|---|---|
| Haiku 5.5 | **$0.25** | **$0.63** | **$1.26** |
| Sonnet 5.5 | **$5.04** | **$12.60** | **$25.20** |
| Opus 5.5 | **$10.08** | **$25.20** | **$50.40** |
| *Sonnet 5.5 via Batch API* | $2.52 | $6.30 | $12.60 |

Scale linearly with source length: a 3-h podcast costs about 3× on the transcript part. Prompt caching of the 3K rubric saves little here, because the transcript dominates and changes every call.

**Suggested setup:** run Haiku 5.5 as a first pass over the whole transcript to propose about 30 windows. Then run Sonnet 5.5 on just those windows (about 6K tokens) to score with the rubric and pick 15–20. That costs about $0.03 per source. This is a design suggestion, not a measured result.

**Add transcription:** Groq turbo at $0.04/h on paid. At 10 h/day that is $12/month, but 10 h/day exceeds the free 8 h/day cap.

---

## (d) Running `claude -p` (Claude Code headless) on a Claude Pro subscription

### What Anthropic's official docs state

1. **Headless mode is a documented feature.** "Add the `-p` (or `--print`) flag to any `claude` command to run it non-interactively." The page shows it in build scripts, CI and cron-style jobs ("Turn off permission prompts in unattended runs… for example in a scheduled job"). [D1]
2. **`-p` uses your subscription login unless you use `--bare`.** The docs say bare mode "doesn't use your subscription login" and "never reads OAuth credentials". So plain `claude -p` *does* use the logged-in Pro account. They also say `--bare` "is the recommended mode for scripted and SDK calls, and will become the default for `-p` in a future release". **If that default changes, Pro-authenticated `-p` scripts would need an API key or an explicit non-bare flag.** [D1]
3. **Subscription sign-in is allowed for your own use.** The legal page says OAuth "is intended exclusively for purchasers of Claude Free, Pro, Max, Team, and Enterprise subscription plans and is designed to support **ordinary use** of Claude Code". It also says the restrictions don't "prevent an end user from signing in to the unmodified Claude Code binary with their own Claude subscription". [D2]
4. **Limits assume ordinary individual use.** "Advertised usage limits for Pro and Max plans assume ordinary, individual usage of Claude Code and the Agent SDK." [D2]
5. **Developers building products must use API keys.** "Developers building products or services… including those using the Agent SDK, should use API key authentication." Anthropic does not permit routing "requests through Free, Pro, or Max plan credentials on behalf of their users." Anthropic "reserves the right to take measures to enforce these restrictions… without prior notice." [D2][D3]
6. **The Consumer Terms (Free/Pro/Max)** prohibit, "Except when you are accessing our Services via an Anthropic API Key or where we otherwise explicitly permit it… to access the Services through automated or non-human means, whether through a bot, script, or otherwise." Effective 2025-10-08. ⚠ possibly stale; a newer version may exist. [D4]
7. **Pro usage limits:** "Both Pro and Max plans have a five-hour session limit and a weekly limit." These are "shared across Claude and Claude Code, meaning all activity in both tools counts against the same limits." If `ANTHROPIC_API_KEY` is set, Claude Code uses it "instead of your Claude subscription", which bills at API rates. **No numeric Pro limit is published.** Third-party estimates (about 10–45 prompts per 5 h) are unofficial. [D5]

### What is NOT stated

- No official page says "you may run `claude -p` from cron on a Pro plan". No page says you may not either.
- The Consumer Terms' "explicitly permit" exception isn't tied to `-p` in any document I found. The headless docs describe scheduled, unattended use but don't say which plan or auth they assume.
- No published per-request or per-token Pro quota, so you can't plan capacity precisely.

### Verdict for ClipForge

- **Gray zone, low risk for occasional personal use:** you running the unmodified CLI, signed in as yourself, for your own clips, a few calls a day, is what the docs call "ordinary, individual usage". No official text forbids it.
- **Not clearly sanctioned for an unattended daily pipeline.** The Consumer Terms' automation clause and the "ordinary use" wording point to the API key for scripts. Practical issues: the pipeline shares the 5-hour and weekly limits with your own chat use, has no published quota, and will break if `--bare` becomes the default.
- **Recommendation:** use the **API with Haiku 5.5 or Sonnet 5.5** for the pipeline. That's about $0.25–$25/month (table above). Make sure `ANTHROPIC_API_KEY` is set only in the pipeline's environment. Use `claude -p` on Pro only for ad-hoc, hands-on runs.

---

## Sources (all accessed 2026-10-26)

**Transcription**
- [T1] Groq, Rate Limits, https://console.groq.com/docs/rate-limits (undated)
- [T2] Groq, Supported Models (whisper prices / max file size), https://console.groq.com/docs/models (undated)
- [T3] Groq, Speech to Text guide (file limits, 10 s minimum billing, word timestamps, 16 kHz downsampling, chunking), https://console.groq.com/docs/speech-to-text (undated)
- [T4] Groq Community, "Is there a free tier and what are its limits?" (no card required), https://community.groq.com/t/is-there-a-free-tier-and-what-are-its-limits/790. Third-party free-tier figures: https://www.morphllm.com/groq-pricing (Oct 2026). Groq turbo launch post ($0.04/h), https://groq.com/whisper-large-v3-turbo-now-available-on-groq-combining-speed-quality-for-speech-recognition/ ⚠ possibly stale (2024)
- [T5] OpenAI, Speech to text guide (timestamp_granularities only on whisper-1, 25 MB, deprecations 2027-02-26), https://developers.openai.com/api/docs/guides/speech-to-text
- [T6] OpenAI, API pricing (transcription per-minute rates), https://developers.openai.com/api/docs/pricing
- [T7] OpenAI, API reference, create transcription, https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create
- [T8] Deepgram, Pricing ($200 credit, no card, per-minute rates, concurrency), https://deepgram.com/pricing
- [T9] SYSTRAN faster-whisper README (CPU benchmark, word_timestamps, BatchedInferencePipeline), https://github.com/SYSTRAN/faster-whisper
- [T10] faster-whisper issue #1030 (turbo vs medium anecdote), https://github.com/SYSTRAN/faster-whisper/issues/1030 ⚠ possibly stale (Oct 2024)
- [T11] Deepgram docs, pre-recorded audio (2 GB, timeouts), https://developers.deepgram.com/docs/pre-recorded-audio. Whisper Cloud (10-min timeout), https://developers.deepgram.com/docs/deepgram-whisper-cloud
- [T12] AssemblyAI, Pricing, https://www.assemblyai.com/pricing
- [T13] AssemblyAI FAQ, file size / duration limits, https://www.assemblyai.com/docs/faq/are-there-any-limits-on-file-size-or-file-duration-for-files-submitted-to-the-api
- [T14] AssemblyAI FAQ, word timestamps, https://www.assemblyai.com/docs/faq/does-your-api-return-timestamps-for-individual-words
- [T15] DeepInfra, whisper-large-v3-turbo model page, https://deepinfra.com/openai/whisper-large-v3-turbo

**Claude**
- [C1] Anthropic, Pricing, https://platform.claude.com/docs/en/about-claude/pricing (docs.anthropic.com redirects here)
- [D1] Claude Code docs, "Run Claude Code programmatically" (headless / `-p` / `--bare`), https://code.claude.com/docs/en/headless
- [D2] Claude Code docs, Legal and compliance (OAuth "ordinary use", "ordinary, individual usage", developer restrictions), https://code.claude.com/docs/en/legal-and-compliance
- [D3] Claude Code docs, Agent SDK overview (third parties may not offer claude.ai login/rate limits), https://code.claude.com/docs/en/agent-sdk/overview
- [D4] Anthropic, Consumer Terms of Service, effective 2025-10-08, https://www.anthropic.com/legal/consumer-terms ⚠ possibly stale
- [D5] Claude Help Center, "Using Claude Code with your Pro or Max plan" (5-hour + weekly limits, shared limits, API key precedence; page marked "Updated this week"), https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan
