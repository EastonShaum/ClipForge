# Hooks Playbook: what makes short clips perform (2026)

Researched 2026-10-26 for ClipForge. All sources accessed 2026-10-26. Anything published or last confirmed before 2026-04-26 is marked **⚠ possibly stale**.

## TL;DR

- **The first 1–3 seconds decide distribution.** TikTok's own ad guidance says to state your point in the first 3 seconds. A 2026 analysis of about 34.6K viral TikToks found the first frame must be concrete (a number, a claim, a reaction) and visibly moving, not a slow intro. Clip in at the strongest line. Never clip in at "so, um, anyway". [S1][S2]
- **A clip must stand on its own and pay off.** A clip that needs earlier context, or that never resolves its setup, loses viewers mid-way. YouTube's reused-content rule also requires real editorial value (story, commentary), not raw dumps. [S2][S9]
- **Length: make 20–45 s the default and allow up to 60–90 s for a strong story.** Shorts and Reels go up to 3 min. TikTok's Creator Rewards pays only for videos over 1 min, so a TikTok-monetisation cut should run 61–90 s. X allows 140 s on free accounts. [S4][S5][S6][S7]
- **Burned-in captions are required.** Use 1–3 words at a time, large, high-contrast and centred in the safe zone, with the active word highlighted. The first caption must appear within about 0.5 s. [S1][S2][S10]
- **Hard rejects:** clips that start mid-sentence, need prior context, have no payoff, or contain sponsor reads, gambling/casino or referral codes, or slurs. Gambling is a live policy risk on YouTube (March 2025) and Twitch (2022–23). [S11][S12]

---

## 1. Platform facts that constrain clip selection

| Platform | Max length (vertical) | Practical sweet spot for clips | Notes | Source |
|---|---|---|---|---|
| YouTube Shorts | 3 min (videos uploaded after 2024-10-15; square or vertical only) | 20–60 s | Public view counts changed in March 2025 to count every start or replay. Use "Stayed to watch" / "viewed vs swiped away" in Studio as the hook metric. | [S4] ⚠ policy dates from 2024; [S3] |
| TikTok | Long uploads allowed | 20–45 s for reach. 61–90 s if you want Creator Rewards eligibility (>1 min, original, not sponsored) | CRP thresholds per third parties: 18+, 10K followers, 100K views in 30 days. Not confirmed on an official TikTok page. | [S6] (third-party, 2026) |
| Instagram Reels | 3 min is the widely available cap. Some accounts report 15–20 min upload rollouts. | 15–45 s | Ranking signals Mosseri named most often: watch time, likes per reach, **sends per reach**. Sends matter most for non-followers. | [S5] (third-party, 2026), [S8] ⚠ Jan 2025 statement |
| X | 140 s for non-Premium. Premium: up to 4 h on web/iOS, 10 min on Android. | 20–60 s | Keep a 140 s cap in the renderer if posting from a free account. | [S7] |

**What to do:** have the LLM target 25–55 s. Allow 15–25 s for a single-beat gaming moment and 60–90 s for a full story. Render a separate ">61 s" cut only when a TikTok-monetised account is in play.

## 2. Hook taxonomy (first 2 seconds)

A hook is the **first spoken line or first visual beat of the clip**, not a line from later in the source. ClipForge picks start timestamps, so the hook has to *exist in the transcript* at or within about 1 s of the clip start. The renderer can also add an on-screen text hook (§4) to make up for a weak first spoken line.

| `hook_type` | What it is | Podcast/interview example | Gaming example |
|---|---|---|---|
| `bold_claim` | A contrarian or strong statement | "College is the worst investment you can make right now." | "This is the most broken gun in the game and nobody's using it." |
| `specific_number` | A concrete figure, amount or time up front | "I lost $40,000 in one afternoon." | "Rank 1 in 9 hours, here's how." |
| `question` | A direct question the clip answers | "Why do 90% of restaurants fail in year one?" | "How did he not see me?" |
| `story_open` | Drops into the middle of a narrative | "So I'm sitting in the police car, right…" | "Last circle, 1 HP, no heals —" |
| `conflict` | Disagreement or tension between speakers | "No, that's just wrong, and here's why." | Teammate or chat argument, "Why would you push that?!" |
| `reaction` | A strong emotional outburst is the first beat | Guest bursts out laughing, or a shocked "Wait, what?" | Scream, rage or disbelief ("NO WAY") |
| `reveal` | Answers a mystery or reveals a secret or result | "The real reason I left the company was…" | Clutch result, rare drop or unboxing outcome |
| `curiosity_gap` | Promises information it hasn't given yet | "There's one thing every rich person I've met does." | "Watch what happens when I open this door." |
| `relatable_pain` | Names a common frustration | "If you've ever been ghosted after a final interview…" | "When your teammate takes the last med kit" |
| `fail_or_absurd` | Something visibly goes wrong or is absurd | A mic falls or a chair breaks | Glitch, ragdoll, accidental self-kill |
| `skill_flex` | An improbable display of skill | (rare in podcasts) | 1v4 clutch, trick shot, speedrun skip |

Sources for patterns: concrete specificity, one emotion, an open loop or promise, motion within the first second, and a cut before the natural end [S2]. State the proposition in the first 3 s [S1]. Clutches, "how is this possible" moments, fails and self-contained gaming moments [S13] (vendor blog). Questions, tension spikes and sharp opinions as podcast clip starts [S10] (vendor blogs).

**Weak openings to avoid:** greetings, "so…", "um", "like I said", "going back to", "yeah, exactly", or a pronoun with nothing earlier to refer to ("he", "that thing") at second 0.

## 3. Caption styles (burned-in)

- **Word-by-word or karaoke highlight**, 1–3 words on screen, active word in an accent colour. This is the most common recommendation for fast talk. For slower, reflective answers, short phrase chunks also work [S10] (vendor blogs, no hard data).
- **First caption on screen by about 0.5 s**, large and high-contrast. One vendor estimates about 40% of first views are sound-off autoplay [S2]. Other vendor claims of ~80% sound-off are unsourced [S10].
- **Text pacing:** TikTok's ad guidance says 5–10 words per second when using text [S1].
- **Placement:** centre or lower-middle, kept clear of the bottom ~20% and right edge where platform UI sits. Burned-in captions stay visible when UI overlays change [S10].
- **Multi-speaker:** give each speaker their own colour so viewers can follow without audio [S10].
- **Don't emphasise every word.** Emphasise numbers, names and the punchline word only [S10].
- **Audio:** clean audio from frame 1, with voice peaks around -6 to -3 dB [S2].

## 4. On-screen text patterns (title card / overlay, first ~3 s)

The LLM should output a short `overlay_text` (≤ 7 words) for each clip. Patterns:

- **Stakes label:** "He bet his house on this" / "1 HP. Last circle."
- **Question label:** "Is college still worth it?"
- **Context patch:** a one-line patch for missing context, such as "Ex-Navy SEAL on fear". Use it only when the transcript doesn't name the speaker or topic.
- **Number label:** "$40K gone in 1 day"

Rules: no clickbait that the clip doesn't pay off, which is the same principle as "make a promise, then deliver" [S2]. No ALL-CAPS paragraphs. Never cover the speaker's face.

## 5. Story arcs and payoff

A good clip has a **complete mini-arc**:

1. **Hook** (0–3 s): claim, question, stakes or reaction.
2. **Build** (3 s to ~80%): one line of context, escalation, specifics.
3. **Payoff** (last ~20%): punchline, reveal, answer, clutch result or strong opinion line.
4. **Out:** cut *right after* the payoff, slightly before the natural rest. Don't include the host's "wow, yeah" wind-down [S2].

Common podcast arcs: *question → surprising answer*, *story → twist*, *claim → evidence → mic-drop line*, *disagreement → resolution or zinger*.
Common gaming arcs: *setup/stakes → attempt → result + reaction*, *calm → sudden chaos → reaction*, *trash talk → instant karma*.

## 6. Loopability

Loops add watch time on every platform (Reels ranks heavily on watch time [S8] ⚠). Tactics that a transcript-based selector can detect:

- **End where the start makes sense again.** The last line is a question the first line answers, or the end lands on the same topic word as the start.
- **Cut on the reaction peak**, not after it, so the restart feels seamless.
- **No outro, no "follow for more" at the end**, because that breaks the loop.

Score loopability as a bonus. Never prefer a loop over a clear payoff.

## 7. Posting times

Sprout Social 2026 data (about 2B engagements, ~307K profiles, collected 2025-11-27 to 2026-02-27, local time) [S14] ⚠ published 2026-03-31:

| Platform | Best windows (local time) | Worst |
|---|---|---|
| TikTok | Tue–Fri 2–6 pm. Wed 1–8 pm is the widest window. | Weekends |
| Instagram | Tue 1–7 pm, Wed 12–9 pm, Thu 12–2 pm | Weekends |
| X | Tue–Thu 12–6 pm | Weekends |
| YouTube Shorts | Sprout gives no Shorts windows. Use YouTube Studio's "when your viewers are on YouTube". | — |

These are averages across brand accounts, so treat them as a starting point. For a scheduler: post 1–3 clips a day spread across 12–8 pm audience-local time, then re-tune from your own analytics after about 30 posts.

## 8. Gaming-stream vs podcast/interview clips

| | Gaming streams | Podcasts / interviews |
|---|---|---|
| What carries the clip | **Visual event + vocal reaction** (scream, laugh, silence then "no way"). The transcript only partly captures it. | **Words**: the claim, story or punchline is in the transcript. |
| Transcript signals to look for | Sudden exclamations, swearing spikes, repeated words ("no no no NO"), laughter tokens, short rapid lines, chat being read aloud, "clip that", "did you see that" | Questions from the host, "the craziest thing…", numbers, named people or brands, disagreement markers ("no", "that's not true"), story openers ("so one time") |
| Typical length | 15–35 s (one beat). Gaming clips of 15–30 s are said to do best for reach [S13] (vendor). | 30–60 s. Up to 90 s for a complete story. |
| Context risk | High. The viewer must understand the game state, so the LLM should prefer moments where the streamer *narrates* the stakes. | Medium. Watch for "he"/"that" referring to things said earlier. |
| Specific rejects | Gambling, slots or case-opening segments with referral codes. Long silent gameplay stretches. Dead air while queueing. Reading donations with real names. | Sponsor reads, ad breaks, "link in description", housekeeping, intros and outros |
| Overlay text | Stakes plus game state ("1 HP, last squad left") | Topic plus speaker credibility ("Ex-FBI negotiator on lying") |
| Policy notes | Twitch bans streaming unlicensed slots/roulette/dice sites and referral links to them (2022, expanded 2023) [S12] ⚠. YouTube (March 2025) bans directing viewers to unapproved gambling sites, including verbal references, and age-restricts casino promotion [S11] ⚠. | YouTube reused-content policy: clips need added value (story, commentary, editing). This applies channel-wide, even with the creator's permission [S9] ⚠ updated July 2025. |

Note on gaming: because the transcript misses visual events, ClipForge should optionally pass an **audio-energy / loudness spike list** to the LLM alongside the transcript. This is a design suggestion, not a sourced claim.

---

## 9. Scoring rubric (paste into the clip-selector prompt)

```text
You are selecting short-form clips (TikTok, YouTube Shorts, Instagram Reels, X) from a
word-timestamped transcript. Propose 15–20 candidates. For each, score every criterion
0–10, compute the weighted total (0–100), and apply the hard rejection rules first.

HOOK_TYPE ENUM (pick exactly one for the first spoken line):
  bold_claim | specific_number | question | story_open | conflict | reaction |
  reveal | curiosity_gap | relatable_pain | fail_or_absurd | skill_flex

CRITERIA (weight × score 0–10; weights sum to 10, so max total = 100)

1. HOOK STRENGTH (weight 2.5)
   Does the first spoken line (first ~2 s) stop a scroller on its own?
   10 = concrete, surprising, emotional or specific ("I lost $40,000 in one afternoon.")
    5 = relevant but generic ("So money is a big topic.")
    0 = filler/greeting/pronoun with no referent ("Yeah, and so he did that.")

2. PAYOFF (weight 2.0)
   Does the clip deliver a punchline, answer, reveal, result, or quotable line before it ends,
   and end within ~1 s after it?
   10 = clear, satisfying payoff in the last 20% of the clip
    5 = payoff present but buried mid-clip or followed by >3 s of wind-down
    0 = setup with no resolution

3. STANDALONE CLARITY (weight 1.5)
   Can a stranger with zero context follow it? Speakers, topic, and stakes are clear
   within the clip (or fixable with ≤7 words of overlay_text).
   10 = fully self-contained
    5 = needs a short overlay to make sense
    0 = depends on earlier discussion

4. EMOTIONAL INTENSITY (weight 1.5)
   Strength of a single emotion: humor, shock, anger, awe, tension, inspiration.
   10 = laughter/shouting/visible disbelief or a genuinely funny/shocking line
    5 = mildly interesting
    0 = flat, informational, monotone

5. PACING / DENSITY (weight 1.0)
   Words-per-second and absence of dead air, filler, tangents.
   10 = no pauses >1.5 s, no tangents, every sentence advances the arc
    5 = some filler or one tangent
    0 = long pauses, rambling, repeated restarts

6. SHAREABILITY / RELATABILITY (weight 1.0)
   Would someone send this to a friend or argue in the comments? (Sends are a key Reels signal.)
   10 = strong opinion, relatable situation, or "you have to see this" moment
    0 = niche, inside-baseball, or only meaningful to existing fans

7. LENGTH FIT (weight 0.5)
   10 = 20–55 s (gaming single beat: 15–35 s; full story: up to 90 s is OK)
    5 = 55–90 s, or 12–20 s
    0 = <12 s or >120 s

LOOPABILITY BONUS (+0 to +3 added after weighting, total capped at 100)
   +3 if the last line flows naturally back into the first line or ends on the reaction peak
   with no outro.

HARD REJECTION RULES (reject regardless of score; record reject_reason):
  R1 starts_mid_sentence   – first word is not the start of a sentence/thought, or the clip
                              starts on a filler/connector ("and", "so", "but", "um", "like",
                              "yeah", "exactly", "right").
  R2 ends_mid_sentence     – last word cuts off a sentence or thought.
  R3 needs_prior_context   – relies on an unexplained reference ("as I said", "that guy",
                              "going back to it") that overlay_text cannot fix in ≤7 words.
  R4 no_payoff             – PAYOFF score < 4.
  R5 sponsor_or_ad         – contains a sponsor read, ad break, promo code, "link in
                              description", Patreon/merch plug, or affiliate mention.
  R6 gambling              – contains gambling/casino/slots/roulette/dice/case-opening/
                              betting-site content, odds promotion, or referral codes.
  R7 unsafe_or_hateful     – slurs, harassment of a private person, sexual content,
                              graphic violence, self-harm instructions, doxxing (real names
                              /addresses of private people, donor names read aloud).
  R8 dead_air              – contains a silence gap > 3 s (gaming: > 4 s with no reaction).
  R9 out_of_bounds         – duration < 10 s or > 180 s.
  R10 duplicate            – overlaps another selected clip by > 30% of its duration
                              (keep the higher-scoring one).

BOUNDARY RULES:
  - start_time = start of the first word of the hook sentence, minus 0.15 s.
  - end_time   = end of the last word of the payoff sentence, plus 0.3 s.
  - Never start on the host's question if the answer alone is self-contained AND stronger;
    otherwise include the question (it is often the best hook).

OUTPUT per candidate (JSON):
  { "start": float, "end": float, "hook_type": <enum>, "hook_line": str,
    "payoff_line": str, "overlay_text": str (≤7 words), "scores": {criterion: int},
    "loop_bonus": int, "total": float, "rejected": bool, "reject_reason": str|null,
    "why": str (≤25 words) }
Return candidates sorted by total, rejected ones last.
```

### Worked examples (for calibration)

- **Podcast, accept (~84):** starts "I got fired on my first day at Google." (specific_number/story_open, hook 9) → 40 s story → ends "…and that's how I ended up running the team that fired me." (payoff 10, standalone 9, emotion 7, pacing 8, share 8, length 10) plus a loop bonus of 1.
- **Podcast, reject R3:** starts "Right, and that's why the second one was worse." The referent is earlier in the episode.
- **Gaming, accept (~78):** "1 HP, no heals, three of them left —" (story_open) → 18 s clutch → "NO WAY. NO WAY! Clip that!" (reaction payoff 9, emotion 10, standalone 7 with the overlay "1v3, 1 HP").
- **Gaming, reject R6:** the streamer opens a case-opening site and reads a referral code. Reject even if the reaction is huge.
- **Any, reject R5:** "This episode is brought to you by…" appears anywhere in the window.

---

## Sources (all accessed 2026-10-26)

- [S1] TikTok Ads, "Creative best practices for performance ads", https://ads.tiktok.com/help/article/creative-best-practices?lang=en (undated page; guidance is for ads and applied here by analogy)
- [S2] OpusClip, "8 Things All Viral TikToks Do in the First 3 Seconds (2026)", published 2026-05-12, https://www.opus.pro/blog/viral-tiktoks-first-3-seconds-2026 (vendor analysis of 34,635 TikToks; numbers not independently verified)
- [S3] Bytecap, "YouTube Shorts retention benchmarks", updated 2026-07-17, https://www.bytecap.io/research/youtube-shorts-retention-benchmarks. Also ContentStudio, https://contentstudio.io/blog/youtube-shorts-analytics-guide (third-party; describes "Stayed to watch" and the March 2025 view-count change)
- [S4] YouTube Help, "Shorts up to three minutes", https://support.google.com/youtube/answer/15424877 ⚠ possibly stale (rule dates from 2024-10-15; page still live)
- [S5] Inro, "Instagram Reels Time Limit in 2026", https://www.inro.social/blog/instagram-reels-can-now-be-20-minutes-long-new-time-limit-explained-2025. Kontentino, https://www.kontentino.com/q-and-a/how-long-can-instagram-reels-be/ (third-party; sources disagree, no official Instagram page found)
- [S6] ttcalculator, "TikTok Creator Rewards Program: Requirements (2026)", https://ttcalculator.net/guides/creator-rewards-program/ (third-party, cites US terms updated July 2026)
- [S7] X Help Center, "About longer videos for X Premium subscribers", https://help.x.com/en/using-x/premium-longer-videos
- [S8] eclincher, "How the Instagram Algorithm Works in 2026", https://www.eclincher.com/articles/how-the-instagram-algorithm-works-in-2026 (secondary; reports Mosseri's January 2025 signal list ⚠ possibly stale)
- [S9] YouTube Help, "YouTube channel monetization policies" (reused/inauthentic content), https://support.google.com/youtube/answer/1311392 ⚠ possibly stale (last policy change 2025-07-15)
- [S10] Vendor guides on podcast clips and captions: Quso, https://quso.ai/blog/how-to-edit-podcast-shorts-like-a-pro.html. Klap, https://klap.app/blog/create-shorts-from-podcast. Swell AI, https://www.swellai.com/blog/how-to-clip-a-podcast-segment (vendor claims, weak evidence)
- [S11] ABC7/CNN, "YouTube cracks down on gambling videos…", March 2025, https://abc7news.com/post/youtube-cracks-down-gambling-videos-trying-lure-viewers-unapproved-sites/15976808/ ⚠ possibly stale
- [S12] CNBC, "Twitch announces ban on unlicensed gambling livestreams", 2022-09-21, https://www.cnbc.com/2022/09/21/twitch-announces-ban-on-unlicensed-gambling-livestreams-after-backlash-.html. NBC News on the 2023 expansion, https://www.nbcnews.com/tech/internet/twitch-expands-ban-gambling-livestreams-also-says-viewership-content-7-rcna97943 ⚠ possibly stale
- [S13] Eklipse, "Best games to stream for viral clips", https://blog.eklipse.gg/basic-knowledge/best-games-to-stream-for-viral-clips.html. Captions.ai gaming use-case, https://captions.ai/help/use-cases/gaming (vendor claims)
- [S14] Sprout Social, "Best times to post on social media in 2026", published 2026-03-31, https://sproutsocial.com/insights/best-times-to-post-on-social-media/ ⚠ possibly stale (before 2026-04-26)

**Evidence quality note:** platforms publish very little official hook or retention data. Most numbers here come from vendors (OpusClip, Eklipse, Quso) that sell clipping tools. Treat the rubric weights as a starting point, and re-weight after about 50 posted clips using your own "stayed to watch" and average-percentage-viewed data.
