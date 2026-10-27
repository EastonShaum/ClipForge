"""Brief stage: read a campaign's requirements off a screenshot of its brief / requirements doc.

Touches no DB. Applying what it finds to the campaign is a separate, user-confirmed step
(clipforge/briefs.py)."""

import base64
from pathlib import Path

from pydantic import BaseModel

from clipforge.config import Settings
from clipforge.costs import llm_cost

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
music, minimum views, submission steps)"""


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


def read_brief(s: Settings, image_path: Path) -> tuple[dict, dict]:
    if s.select_provider == "fake":
        return _fake(), {"provider": "fake", "model": "fake", "usd": 0.0,
                         "tokens_in": 0, "tokens_out": 0}
    if s.select_provider != "anthropic":
        raise ValueError("reading screenshots needs CF_SELECT_PROVIDER=anthropic")
    import anthropic

    from clipforge.stages.transcribe import MissingKey

    if not s.anthropic_api_key:
        raise MissingKey("CF_ANTHROPIC_API_KEY is not set")
    client = anthropic.Anthropic(api_key=s.anthropic_api_key, max_retries=4)
    img = base64.standard_b64encode(image_path.read_bytes()).decode()
    resp = client.messages.parse(
        model=s.select_model,
        max_tokens=3000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": img}},
            {"type": "text", "text": PROMPT},
        ]}],
        output_format=Brief,
    )
    tin, tout = resp.usage.input_tokens, resp.usage.output_tokens
    usd = llm_cost(s.select_model, tin, tout)
    return resp.parsed_output.model_dump(), {"provider": "anthropic", "model": s.select_model,
                                             "tokens_in": tin, "tokens_out": tout, "usd": usd}


def estimate_usd(s: Settings) -> float:
    return llm_cost(s.select_model, 2500, 1000) if s.select_provider == "anthropic" else 0.0


def _fake() -> dict:
    return Brief(summary="Clip the episode's best stories.", required_tags=["#manon", "@manon"],
                 caption_text="", disclosure="", platforms=["tiktok", "youtube"],
                 min_seconds=-1, max_seconds=-1, end_date="2026-11-15",
                 footage_links=["https://drive.google.com/x"],
                 logo_rule="Man On and Playmaker logos must be clearly visible.",
                 rules=["No profanity in the hook", "English only"]).model_dump()
