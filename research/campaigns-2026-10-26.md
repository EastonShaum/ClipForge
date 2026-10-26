# Clipping.net campaigns: snapshot 2026-10-26

Taken from Easton's 8 dashboard screenshots (taken 2026-10-26) and the one requirement doc we could read. Clipping.net-hosted requirement docs (`clipping.net/campaigns/doc/...`) are behind a Vercel bot checkpoint. We don't bypass it, so Easton copies those docs in by hand.

## How Clipping.net is structured (new facts)
- A **campaign** (the brand/creator) holds several **bounties**. Each bounty has its own rate, requirement doc, budget bar and Active/Disabled/Ended state. **In ClipForge, enter each bounty as its own campaign** (e.g. "Man On – episode-4").
- Two bounty types:
  - **Rate:** $X per 100K views. All the bounties shown are 100K-based, from $50 to $250/100K.
  - **Pot:** a fixed pot shared by view share, with a minimum view count (e.g. $20K pot, min 1.5M views). Not modeled in ClipForge, and not worth chasing as a beginner.
- **A bounty closes when its budget fills.** Speed matters, so being early on a new bounty beats a high rate on a 94%-full one.
- Payouts are via **PayPal** for most campaigns, and **USDT/USDC on Ethereum** for some (Preme, Lacy). Lacy warns that clippers without a USDC address set get kicked.
- "Account limit: Unlimited" everywhere, so you may submit from more than one social account.
- Bounties are tagged "English-speaking", which refers to the account's audience.
- None of the campaigns shown is gambling or casino.

## Bounty table
"Implied budget" is views × rate ÷ % filled. It's a rough estimate: it assumes every tracked view was paid. Avg views/clip is the mean, which a few viral clips inflate; the median is far lower.

| Campaign · bounty | Rate /100K | Pay | Filled | Clips · views | Avg views/clip | Implied budget left | Fit |
|---|---|---|---|---|---|---|---|
| **GFuel · gfuel-2** | $125 | PayPal | 43% | 499 · 2.56M | ~5.1K | ~$4.2K | ✅ gaming-adjacent, brand says it will expand the budget |
| **Man On · episode-4** | $125 | PayPal | ~30% | 271 · 1.17M | ~4.3K | ~$3K+ | ✅ podcast; footage supplied (doc read) |
| **Man On · man-on-episode-1** | $125 | PayPal | 46% | 527 · 1.83M | ~3.5K | ~$2.7K | ✅ podcast |
| **Brandon Joe Williams · contract-killer** | $150 | PayPal | 1% | 18 · 23K | ~1.3K | ~$3.4K | ⚠️ highest rate and nearly empty, but read the doc first: the creator's content may be legal/financial claims that platforms flag |
| Clix & Lacy – TapCap · gamestop | $110 | PayPal | 94% | 152 · 4.71M | ~31K | ~$330 | ❌ gaming, but almost full |
| Preme · preme | $50 | USDT (ETH) | 73% | 86 · 5.83M | ~68K | ~$1.1K | ❌ music, low rate, crypto payout with gas fees |
| Lacy | — | USDC (ETH) | no active bounties | | | | watch |
| Tubi | — | PayPal | no active bounties (last was $250/100K) | | | | watch: new bounties pay well |
| Jared Fried | — | PayPal | no active bounties | | | | watch |
| Man On · 3 more bounties | $125 | PayPal | not visible ("View all (6)") | | | | check |

## Man On · episode-4 requirements (read 2026-10-26)
- Audience: English.
- Footage: one Google Drive file, `Man On_Ep4_Dennis_V2.mp4`, 39:18. This is supplied footage, so it's authorized.
- **The Man On logos (Drive folder) must be in the clip and CLEARLY VISIBLE.** ClipForge can't do this yet; see the gap below.
- The caption must include `#manon`.
- No disclosure wording is specified, so our `#ad` plus the on-screen "Paid partnership #ad" stays.
- The Drive file answered "Too many users have viewed or downloaded this file recently". Popular campaign files hit Drive's download quota. The workaround: download in the browser (or "Make a copy" into your own Drive), then `cf source <id> file:///path`.

## What this answers in QUESTIONS.md
- Q2 (platforms): not shown on campaign pages; it's in each bounty's doc. Man On ep-4 doesn't restrict platforms. Keep the default of TikTok + YouTube, logged separately.
- Q3 (disclosure): Man On ep-4 sets none, so the default stands. Briefs do set **required hashtags** and **logo overlays**.
- Payouts: PayPal for every recommended bounty. A crypto wallet is only needed for Preme or Lacy, so skip those for now.

## Gap found: brand logo overlay
Man On requires its logos to be clearly visible, and other briefs likely do too. The renderer has no logo option. Needed: a per-campaign logo PNG, with position and size, burned in by ffmpeg `overlay`. It must not collide with the hook box (top third) or the disclosure (top-left); top-right under the safe zone is the likely spot. About 1 h of work.
