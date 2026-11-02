# Questions (non-blocking)

Review these at a checkpoint. Each has a default I'm using until you answer.

1. **Pro CLI vs API key for selection?** The CLI path works but sits in a Terms-of-Service gray area for unattended use (D9). *Default: API key with Haiku 5.5, under $1/month.*
2. **Which platforms does each campaign pay on, and do you submit each platform's post separately?** This changes how many posts per clip count. *Default: TikTok + YouTube, logged separately.* **2026-10-26:** campaign pages don't say; it's per-bounty in the requirement doc. Man On ep-4 sets no platform limit.
3. **Do campaign briefs set their own disclosure wording** (e.g. "#sponsored", tagging the brand)? *Default: "#ad" first in the caption plus the on-screen "Paid partnership #ad". It can be edited per campaign.* **2026-10-26:** Man On ep-4 sets no disclosure wording, so the default stands. Briefs do require hashtags (`#manon`) and visible brand logos.
4. **Personal brand or business name for the main account?** Research suggests a creator-agnostic name such as `highlightmill`. *Default: creator-agnostic.* **Answered:** @ggcutroom (TikTok + YouTube).
5. **Are you 18+ and able to pass Whop's ID check?** That's needed for the second marketplace. *Default: Clipping.net only until you confirm.*
6. **Is it OK to spend ~$15 on one month of OpusClip as a quality benchmark** against our clips? *Default: no; compare by eye against top clippers' posts instead.*
7. **Do you want a short note on taxes** (1099-NEC/K thresholds, setting aside ~25–30%)? *Default: a one-paragraph reminder in the launch playbook.*
8. **Tailscale HTTPS:** enable MagicDNS + HTTPS certificates in the Tailscale admin console so I can switch to `tailscale serve`? It fixes clipboard copy on iOS. *Default: plain http plus the copy fallback.*
9. **YouTube Data API key** (free) for automatic Shorts view counts? *Default: manual view entry.*
10. **Face-tracking crop priority:** worth a day of work once real footage shows where blurfill looks weak? *Default: after the first 2 weeks of real reviews.*
- **View odds (D38):** the brief's seed says 30% of posts reach 10K views, but also that the median TikTok post gets ~500. Together they give a steep jump between 500 and 10K, so Scout's expected $/clip is probably high until the accounts have 30 posts of their own. Lower `CF_VIEW_ODDS` (e.g. 10K → 10%) if the numbers look rosy.
- **Content Rewards' real fee:** the terms say 10% (0% for per-post/retainer at $5,000+). Compare with the payouts entered on posts; change `CF_CR_FEE` if they differ.

