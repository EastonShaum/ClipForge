# Launch playbook

How to go from zero to a steady routine, and when to quit. Numbers come from `research/`. Adjust them after real data.

## 1. Before day 1 (one evening, ~2 h)
- [ ] Clear BLOCKERS B1–B4: Tailscale, Anthropic key, Groq key, Discord webhook.
- [ ] Install Tailscale on your phone, sign in to the same tailnet, and open `http://raspberrypi:8420`. Add it to your home screen.
- [ ] Choose a handle (research/account-names.md) and claim it on TikTok, YouTube, Instagram and X today, even though Instagram and X wait.
- [ ] In Clipping.net (Discord), pick **2 campaigns**, ideally one podcast/interview and one gaming. Prefer:
  - effective rate **≥ $0.40/1K** (≥ $40 per 100K);
  - minimum views **≤ 10K–25K** per clip (new accounts rarely hit 100K+ early);
  - footage supplied as YouTube links or a Drive folder;
  - an end date at least 3 weeks away;
  - no gambling. The blocklist enforces this anyway.
- [ ] Enter each campaign on the **Campaigns** page with its exact required tags and caption text.
- [ ] Queue 2–3 source videos per campaign on the **Sources** page. By morning you'll have clips to review.

## 2. Days 1–14: account warm-up and first posts
Follow the day-by-day table in `research/account-ramp-up.md` §6. In short:

| Days | TikTok | YouTube Shorts | Instagram | X |
|---|---|---|---|---|
| 1–3 | Set up, 20–30 min/day of niche watching, follow and comment | Same, ~15 min/day | Reserve handle, Creator account, browse | Reserve handle |
| 4–7 | **1 post/day**, reply to every comment | 1 post/day, tick "Paid promotion" | — | optional |
| 8–14 | **2 posts/day** if no notices | 1–2/day | Prep a Reels batch | 1/day only if a campaign pays for X |
| 15+ | 2–4/day | 2–3/day | Start 1 every 1–2 days | per campaign |

Stop rule: any "unoriginal" or "ineligible for recommendation" notice means pausing that platform for 48 h, appealing if it's wrong, and making the next clips more transformed. Never delete and re-upload the same clip.

## 3. Daily routine (target 25–35 min)
| When | What | Time |
|---|---|---|
| Morning | Discord ping → **Review** page: approve or reject every clip, always picking a reason | 5 min |
| Morning | **Post** page: save video, copy caption, upload to TikTok and Shorts, flip the disclosure toggle, paste each URL back | 10–15 min |
| Midday | Second posting slot (week 2+) | 5–10 min |
| Evening | Queue tomorrow's footage on **Sources** (new campaign uploads come in automatically if the campaign gave a channel feed) | 3 min |
| Evening | Submit qualifying posts to the campaign dashboard and reply to comments | 5 min |

## 4. Weekly review (Sunday, 20 min)
- [ ] **Views page:** enter current views for every post from the last 14 days, then check each post's campaign dashboard.
- [ ] **Dashboard → Campaigns:** is the best clip getting close to each threshold? Has the campaign's budget run out?
- [ ] **Dashboard → By hook type:** which 2 hook types have the most views per post? Note them in a rejection note or in the prompt.
- [ ] **Dashboard → By prompt version:** once each version has 30+ reviewed clips, set the loser's weight to 0 (see the runbook).
- [ ] **API spend** is under budget.
- [ ] `scripts/backup-db.sh` has run (also scheduled; see the runbook).
- [ ] Apply the kill criteria below.

## 5. Kill criteria (be ruthless)
| Signal | Threshold | Action |
|---|---|---|
| A campaign has no clip past its minimum after **30 posts or 14 days** | 0 qualifying clips | Drop the campaign |
| A campaign's budget is exhausted, or payouts are more than 60 days late | — | Pause it and post elsewhere |
| A niche's median views stay **< 500/post** after 40 posts | — | Switch niche or hook style before adding volume |
| Your approval rate stays **< 25%** after 2 weeks | — | Selector problem: switch to Sonnet 5.5 (`CF_SELECT_MODEL`), tighten the prompt, or try different footage |
| Two originality strikes on one account | — | Stop posting there for a week and rethink the edit style |
| **Business-level** | see below | Stop or pivot |

**The money test (end of month 3).** Track your hours; the UI routine should be about 30 min/day plus the weekly review, roughly **17 h/month**. Your freelance rate is $50/h.
- **Below $5/hour** (< ~$85/month) and not clearly rising month over month: **stop**, or keep the pipeline only as a portfolio piece.
- **$5–$25/hour:** keep going only if month-over-month growth is above 50%, or if you value the learning.
- **$50/hour or more** (≈ $850+/month): it competes with freelance work, so scale volume (more campaigns, a second account) before anything else.

Taxes: platform payouts are self-employment income. Set aside ~25–30% and keep records; the dashboard's per-campaign estimates help.

## 6. Scaling, once something works
1. Double posting frequency on the winning platform and hook type.
2. Add the second marketplace (Whop).
3. Add Instagram Reels (week 3+), and X only for X-paying campaigns.
4. Consider a second niche account. One person may run several accounts, but don't post identical clips across them (research/account-ramp-up.md).
5. Code stretch goals: face-tracking crop, automatic YouTube view tracking.
