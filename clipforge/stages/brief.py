"""Brief stage: read a campaign's requirements off a screenshot of its brief / requirements doc.

Touches no DB. Applying what it finds to the campaign is a separate, user-confirmed step
(clipforge/briefs.py)."""

from pathlib import Path

from pydantic import BaseModel

from clipforge.config import Settings
from clipforge.costs import llm_cost
from clipforge.stages import screenshot
from clipforge.stages.llm import structured

PROMPT = """This is a screenshot of the requirements ("brief") for a paid clipping campaign: \
clippers cut short videos from the brand's footage and get paid per view.

Extract only what the screenshot actually says. Leave a field empty ("", [] or -1) when it's not \
shown; never guess or fill in common practice.

- summary: one sentence on what the campaign wants
- required_tags: hashtags and @mentions that must appear in the caption, each with its # or @
- caption_text: any other text the caption must contain
- disclosure: the exact ad disclosure wording the brief requires, if it names one
- platforms: allowed platforms, lowercase, from: tiktok, youtube, instagram, x
- min_seconds, max_seconds: allowed clip length
- end_date: YYYY-MM-DD if the brief gives a deadline or end date
- footage_links: URLs for the footage, assets or logos (Drive, Dropbox, YouTube...)
- logo_rule: what the brief says about logos or watermarks (required, banned, where), in a sentence
- rules: every other do or don't, each under 15 words (topics to avoid, language, content style, \
music, minimum views, submission steps)
- campaign_type: cpm, per_post, retainer or prize_pool ("" if not shown)
- rate_per_1k: dollars per 1,000 views (or the fee per post for per_post); min_payout_usd, \
max_payout_usd (per clip), bonus_usd (flat fee per clip), budget_total_usd, budget_remaining_usd
- audience_rules: audience requirements, e.g. "50%+ US viewers"
- footage_scope: what footage may be clipped if limited, e.g. "the sponsor segment only"
- people: the people the campaign is about, as fans know them, with a nickname in brackets \
when the brief gives one, e.g. "StableRonaldo (Ron)"
- must_mention: names the caption or on-screen text must mention, if the brief requires it \
(e.g. ["Core Boys", "Lacy"])
- ai_video_banned and onscreen_ad_label: "yes" or "no" if the brief says (AI-generated video \
banned; an on-screen ad label required), else """""


class Brief(BaseModel):
    # No Optional fields: structured outputs reject schemas with many unions (see D24).
    summary: str
    required_tags: list[str]
    caption_text: str
    disclosure: str
    platforms: list[str]
    min_seconds: float
    max_seconds: float
    end_date: str
    footage_links: list[str]
    logo_rule: str
    rules: list[str]
    campaign_type: str
    rate_per_1k: float
    min_payout_usd: float
    max_payout_usd: float
    bonus_usd: float
    budget_total_usd: float
    budget_remaining_usd: float
    audience_rules: str
    footage_scope: str
    ai_video_banned: str
    onscreen_ad_label: str
    people: list[str]
    must_mention: list[str]


def read_brief(s: Settings, image_path: Path) -> tuple[dict, dict]:
    if s.select_provider == "fake":
        return _fake(), {"provider": "fake", "model": "fake", "usd": 0.0,
                         "tokens_in": 0, "tokens_out": 0}
    if s.select_provider != "anthropic":
        raise ValueError("reading screenshots needs CF_SELECT_PROVIDER=anthropic")
    from clipforge.stages.transcribe import MissingKey

    if not s.anthropic_api_key:
        raise MissingKey("CF_ANTHROPIC_API_KEY is not set")
    if image_path.suffix == ".txt":  # a brief pasted as text
        content = (PROMPT.replace("a screenshot of", "the text of").replace("the screenshot",
                                                                            "the text")
                   + "\n\n<brief>\n" + image_path.read_text(encoding="utf-8") + "\n</brief>")
    else:
        content = screenshot.content(image_path, PROMPT)
    brief, meta = structured(s, Brief, content, 32000)
    return brief.model_dump(), meta


def estimate_usd(s: Settings) -> float:
    return llm_cost(s.select_model, 10000, 1500) if s.select_provider == "anthropic" else 0.0


def _fake() -> dict:
    return Brief(summary="Clip the episode's best stories.", required_tags=["#manon", "@manon"],
                 caption_text="", disclosure="", platforms=["tiktok", "youtube"],
                 min_seconds=-1, max_seconds=-1, end_date="2026-11-15",
                 footage_links=["https://drive.google.com/x"],
                 logo_rule="Man On and Playmaker logos must be clearly visible.",
                 rules=["No profanity in the hook", "English only"], campaign_type="",
                 rate_per_1k=-1, min_payout_usd=-1, max_payout_usd=-1, bonus_usd=-1,
                 budget_total_usd=-1, budget_remaining_usd=-1, audience_rules="",
                 footage_scope="", ai_video_banned="", onscreen_ad_label="",
                 people=["Man On"], must_mention=[]).model_dump()
