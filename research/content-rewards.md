# Content Rewards: what the primary sources say (checked 2026-11-02)

Source: Content Rewards creator terms, https://contentrewards.com/terms

- **API / export:** none for creators. The terms only describe the read-only social-account
  link CR uses to verify ownership and read performance (§14.2). Campaign info therefore comes
  from Scout screenshots or manual entry. A third-party GitHub PR also notes the help center
  documents no public API.
- **Clipper fee (§5):** 10% of every CPM and prize-pool payout. Per-post and retainer: 10% on
  campaigns with a total budget under $5,000, 0% at $5,000 or more. Promotional discounts can
  override it per campaign. → `CF_CR_FEE`, `CF_CR_FEE_FREE_BUDGET_USD`.
- **Views (§4.1, §7.7, §14.1):** verified through the platforms' APIs (Instagram, Facebook,
  TikTok, YouTube, X). Tracking normally runs 30 days from submission. The terms do not say
  which view metric (e.g. YouTube engaged vs public views) is paid. Still open: record it per
  bounty (`pays_on`) from each brief.
- **Timing (§7.3, §7.4):** per-post settles shortly after approval; CPM rounds are a 7-day
  earning period plus a 3-day hold; retainers after each cycle; pools after a 3-day hold.
  Whop withdrawal takes ~3–5 business days.
- **Minimum payout:** brands may set a minimum a clip must earn before it's paid (§6.1).

Secondary (unverified): typical rate ~$1/1K views (guides cite a $1 median over 529 live
campaigns); brands pay a 10% fee (8% verified).
- https://github.com/fayisaftha786/-ai-commerce-hub-/pull/177
- https://openclip.app/guides/whop-clipping-guide
