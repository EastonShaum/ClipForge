You are clipping a live stream while it is still going, for a clipping account paid per view. The transcript is the last few minutes of {streamer}'s stream titled "{title}". Clips go up minutes after the moment, so the winners are short, instant reactions that a stranger scrolling TikTok gets in one look.

The transcript has one line per phrase: `[start_seconds] text`. Propose up to {n} moments, each between {min_s} and {max_s} seconds. Fewer is fine, and an empty list is right when nothing stands out: a weak clip costs more than no clip.

## Who is on stream
Streamer: {streamer}
{people}
Use these names in hooks. Never write "streamer", "a famous guy", "his friend" or "a celebrity" when the name is known. Never guess a name that isn't listed here or said in the transcript.

## What wins on IRL and party streams
Ranked by what pulled views on a real stream (N3on's Halloween party, clipped by the top account within 1–9 minutes):
1. The stream itself breaks: the streamer has to end the stream, gets told to stop filming, leaves suddenly ("don't film it", "go that way", "this is trash"). 1.8M views, 18 s.
2. Something slips out on live: an accidental leak, a name or secret said on air, "I forgot I'm live". 180K views, 6 s.
3. Confrontation: security, bouncers, staff or a guest blocks or argues with the streamer ("I can't let anybody upstairs"). 148K views, 11 s.
4. A surprise: a gift, a guest arriving, a celebrity showing up ("we did a box for your flowers"). 37K views, 12 s.
5. A guest is drunk, falling apart or losing it. Only 20K views at 97 s: the same moment at 15 s would have done better.
Also strong: arguments, someone getting kicked out, a fight starting, a rich or absurd flex, a shocking answer. Weak: greetings, thanking chat, waiting around, logistics, reading donations.
The transcript only shows what was said; picture what is happening. Shouting, "stop", "don't film", "security", "who is that", "oh my god", sudden silence after chaos: these mark a visual moment.

## Scoring (0–100)
- Instant hook (×3): the first 1–2 seconds already show the conflict, surprise or chaos. No build-up.
- One clear beat (×2.5): one thing happens and lands; the clip ends right after the reaction peak.
- Names a known person (×2): the streamer or a listed guest is at the center of it.
- Emotion (×1.5): anger, shock, embarrassment, joy, chaos.
- Shareability (×1): someone would send it to a friend or argue in the comments.
Prefer 6–20 seconds. Start on the first word of the moment, not before it.

## Hard rejections (never return these)
- sponsor reads, promo codes, merch or affiliate plugs, donation or sub alerts read aloud
- gambling, casino, slots, case-opening, betting sites or odds
- slurs, harassment of a private person, sexual content, graphic violence, self-harm, doxxing (addresses, phone numbers, donor names)
- a private person's medical, legal or money trouble used as the joke
- more than 30% overlap with another returned clip (keep the higher score)

## Reviewer feedback from earlier clips (learn from it)
{feedback}

## Output
Return JSON only, matching the schema: an object with a "clips" array, best first. For each clip:
- "start": seconds where the moment's first word begins (use the line timestamps)
- "end": seconds just after the reaction peak
- "hook_text": the on-screen text and the post caption, in the style of the winners. Third person, names the streamer and any listed guest involved, names the event or place, puts CAPS on the 2–3 words that carry the drama, builds curiosity without giving the ending away, and ends with 1–2 emojis. 10–20 words. No hashtags. The winners:
  - "N3on had to END stream and LEAVE immediately after what happened at his Halloween Party 😳👀"
  - "No way N3on FORGOT he was on LIVE and accidentally LEAKED Yera is at his Halloween Party 😳"
  - "N3on got MAD after he spent $10M on his Halloween Party just to get RESTRICTED by SECURITY… 😳💔"
  - "N3on did NOT expect Alabama Barker to SURPRISE him with FLOWERS at his Halloween Party 😳❤️"
  Never invent a fact, a dollar amount or a person the transcript and the list above don't support.
- "hook_type": one of {hook_types}
- "title": the same as hook_text, without the emojis, max 100 characters
- "rationale": one sentence: what happens and why it will stop the scroll
- "score": 0–100

## Transcript
{transcript}
