"""Scout stage: read the bounty cards off a screenshot of a campaign marketplace page.

Only reads what the screenshot shows; it never visits the site. Ranking lives in
clipforge/scout.py because it needs the DB (earlier screenshots give the fill speed)."""

import re
from pathlib import Path

from pydantic import BaseModel

from clipforge.config import Settings
from clipforge.costs import llm_cost
from clipforge.stages import screenshot
from clipforge.stages.llm import structured
from clipforge.stages.screenshot import NotAnImage, prepare_image  # noqa: F401 - app imports these

PROMPT = """This is a screenshot of a paid clipping marketplace: Content Rewards \
(contentrewards.com), Clipping.net or Whop. It shows campaign cards or one campaign's brief page; \
each campaign (or each bounty inside one) pays clippers for their clips' views.

List every campaign or bounty whose card is fully visible, or the one campaign a brief page \
describes. Skip cards cut off at the screen edge. Copy numbers exactly as shown and convert \
them: "1.17M" -> 1170000, "23K" -> 23000, "$125" -> 125. Use -1 for any number the screenshot \
doesn't show, and "" or [] for missing text; never guess.

- marketplace: content_rewards, clipping_net, whop, or other (from the logo, layout or URL)
- campaign: the brand or creator name; bounty: the bounty's own name or slug ("" if none)
- status: active, disabled, ended, or unknown
- kind: "rate" (pays $X per N views) or "pot" (a fixed pot shared by view share)
- campaign_type: cpm (pays per views), per_post (a flat fee per approved post), retainer \
(monthly), or prize_pool
- rate_usd and rate_per_views ("$1.00 / 1K views" -> 1 and 1000; "$125 / 100K" -> 125 and \
100000); for per_post, rate_usd is the fee per post and rate_per_views -1
- min_payout_usd: the minimum payout in dollars a clip must earn; max_payout_usd: the maximum \
payout per clip; bonus_usd: a flat-fee bonus per clip, if any
- pot_usd for pot bounties; min_views: a minimum view count, when stated in views
- budget_total_usd and budget_remaining_usd: the campaign budget and what's left ("$1,240 of \
$5,000 paid out" -> total 5000, remaining 3760)
- filled_pct: how much of the budget is used, 0-100, from the budget bar or its label
- end_date: YYYY-MM-DD if shown
- platforms: allowed platforms, from tiktok, youtube, instagram, x
- footage_scope: what footage may be clipped if limited (e.g. "sponsor segment only"), else ""
- clips and views: the totals shown
- payout: paypal, crypto, other, or unknown (look for PayPal, USDT, USDC, ETH logos or text)
- category: podcast, interview, comedy, startup, soccer, gaming, streamer, music, brand, \
political, prediction market, gambling, or other, judged from the name, artwork and description
- notes: anything else that matters in under 15 words (language, audience region, AI ban)"""


NUMBERS = ("rate_usd", "rate_per_views", "pot_usd", "min_views", "filled_pct", "clips", "views",
           "min_payout_usd", "max_payout_usd", "bonus_usd", "budget_total_usd",
           "budget_remaining_usd")
INTS = ("rate_per_views", "min_views", "clips", "views")


class Bounty(BaseModel):
    # Every field required, no Optional: structured outputs reject schemas with many unions.
    # Numbers use -1 for "not shown"; clean() turns that into None.
    marketplace: str
    campaign: str
    bounty: str
    status: str
    kind: str
    campaign_type: str
    rate_usd: float
    rate_per_views: float
    pot_usd: float
    min_views: float
    filled_pct: float
    clips: float
    views: float
    min_payout_usd: float
    max_payout_usd: float
    bonus_usd: float
    budget_total_usd: float
    budget_remaining_usd: float
    end_date: str
    platforms: list[str]
    footage_scope: str
    payout: str
    category: str
    notes: str


class ScoutRead(BaseModel):
    bounties: list[Bounty]


def clean(b: Bounty) -> dict:
    d = b.model_dump()
    for k in NUMBERS:
        v = d[k]
        d[k] = None if v is None or v < 0 else (int(v) if k in INTS else float(v))
    if d["filled_pct"] is not None:
        d["filled_pct"] = min(100.0, d["filled_pct"])
    for k in ("status", "kind", "payout", "category", "marketplace", "campaign_type"):
        d[k] = d[k].strip().lower()
    total, left = d["budget_total_usd"], d["budget_remaining_usd"]
    if d["filled_pct"] is None and total and left is not None:
        d["filled_pct"] = round(max(0.0, min(100.0, 100 * (1 - left / total))), 1)
    d["platforms"] = ",".join(p.lower() for p in d["platforms"]
                              if p.lower() in ("tiktok", "youtube", "instagram", "x"))
    d["end_date"] = d["end_date"] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d["end_date"]) else None
    return d


def read_screenshot(s: Settings, image_path: Path) -> tuple[list[dict], dict]:
    if s.select_provider == "fake":
        return _fake(), {"provider": "fake", "model": "fake", "usd": 0.0,
                         "tokens_in": 0, "tokens_out": 0}
    if s.select_provider != "anthropic":
        raise ValueError("reading screenshots needs CF_SELECT_PROVIDER=anthropic")
    from clipforge.stages.transcribe import MissingKey

    if not s.anthropic_api_key:
        raise MissingKey("CF_ANTHROPIC_API_KEY is not set")
    # A full marketplace page is 40+ bounties at ~200 tokens each, after several thousand
    # tokens of thinking.
    read, meta = structured(s, ScoutRead, screenshot.content(image_path, PROMPT), 64000)
    return [clean(b) for b in read.bounties], meta


def estimate_usd(s: Settings) -> float:
    # Up to ~6 full-detail slices of a tall page; the real cost is logged from usage.
    return llm_cost(s.select_model, 10000, 3000) if s.select_provider == "anthropic" else 0.0


def _fake() -> list[dict]:
    """Two fixed bounties so the flow is testable offline."""
    common = {"kind": "rate", "status": "active", "rate_per_views": 100000, "pot_usd": -1,
              "min_views": -1, "notes": "", "marketplace": "clipping_net", "campaign_type": "cpm",
              "min_payout_usd": -1, "max_payout_usd": -1, "bonus_usd": -1,
              "budget_total_usd": -1, "budget_remaining_usd": -1, "end_date": "",
              "platforms": [], "footage_scope": ""}
    return [
        clean(Bounty(campaign="Man On", bounty="episode-4", rate_usd=125, filled_pct=30,
                     clips=271, views=1170000, payout="paypal", category="podcast", **common)),
        clean(Bounty(campaign="Preme", bounty="preme", rate_usd=50, filled_pct=73, clips=86,
                     views=5830000, payout="crypto", category="music", **common)),
    ]
