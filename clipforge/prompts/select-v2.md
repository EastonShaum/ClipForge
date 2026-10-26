You are selecting short-form clips (TikTok, YouTube Shorts, Instagram Reels) from a timestamped transcript of a {category} video titled "{title}". The clips are posted by a clipping account for a paid campaign, so each one must stand alone for a stranger scrolling past.

The transcript has one line per phrase: `[start_seconds] text`. Propose the {n} best candidates, each between {min_s} and {max_s} seconds. Apply the hard rejection rules first, then score.

## Criteria (score each 0–10, multiply by weight; weights sum to 10, max total 100)
1. HOOK STRENGTH (×2.5): does the first spoken line (first ~2 s) stop a scroller on its own? 10 = concrete, surprising, emotional or specific ("I lost $40,000 in one afternoon."); 5 = relevant but generic; 0 = filler, greeting, or a pronoun with no referent.
2. PAYOFF (×2.0): a punchline, answer, reveal, result or quotable line lands before the end, and the clip ends within ~1 s after it. 0 = setup with no resolution.
3. STANDALONE CLARITY (×1.5): a stranger with zero context can follow who, what, and why it matters (or it's fixable with ≤7 words of hook text).
4. EMOTIONAL INTENSITY (×1.5): one strong emotion — humor, shock, anger, awe, tension, inspiration.
5. PACING (×1.0): no pauses over 1.5 s, no tangents, every sentence advances the arc.
6. SHAREABILITY (×1.0): would someone send it to a friend or argue in the comments?
7. LENGTH FIT (×0.5): 10 = 20–55 s (gaming single beat: 15–35 s); 5 = 55–90 s or 12–20 s.
Loopability bonus: +3 if the last line flows back into the first or ends on the reaction peak with no outro (cap total at 100).

Gaming streams: favor clutch plays, fails, rage, chat-reaction moments and absurd situations; the hook is often the reaction itself. Podcasts/interviews: favor bold claims, specific numbers, personal stories, disagreements and confessions.

## Hard rejections (never return these)
- starts mid-sentence or on a connector/filler ("and", "so", "but", "um", "like", "yeah", "exactly", "right")
- ends mid-sentence
- needs earlier context ("as I said", "that guy", "going back to it")
- payoff score below 4
- sponsor read, ad break, promo code, "link in description", merch/Patreon/affiliate plug
- gambling, casino, slots, roulette, case-opening, betting sites, odds or referral codes
- slurs, harassment of a private person, sexual content, graphic violence, self-harm, doxxing (including donor names read aloud)
- a silence gap over 3 s (gaming: over 4 s with no reaction)
- overlaps another returned clip by more than 30% (keep the higher score)

## Reviewer feedback from earlier clips (learn from it)
{feedback}

## Output
Return JSON only, matching the schema: an object with a "clips" array, best first. For each clip:
- "start": seconds where the first word of the hook sentence begins (use the line timestamps)
- "end": seconds where the last word of the payoff sentence ends
- "hook_text": on-screen hook for the first 3 seconds, max 8 words, written to stop the scroll; it may paraphrase, and must not spoil the payoff
- "hook_type": one of {hook_types}
- "title": post title, max 70 characters
- "rationale": one sentence: why it will perform, naming the payoff
- "score": weighted total 0–100

## Transcript
{transcript}
