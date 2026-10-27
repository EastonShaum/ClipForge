"""Scout stage: read the bounty cards off a screenshot of a campaign marketplace page.

Only reads what the screenshot shows; it never visits the site. Ranking lives in
clipforge/scout.py because it needs the DB (earlier screenshots give the fill speed)."""

import base64
import hashlib
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel

from clipforge.config import Settings
from clipforge.costs import llm_cost
from clipforge.tools import ffmpeg

MAX_EDGE = 1568  # the API downsizes anything larger, so don't pay to upload it

PROMPT = """This is a screenshot of a page on a paid clipping marketplace (e.g. Clipping.net or \
Whop): campaigns, each with one or more bounties that pay clippers for views.

List every bounty whose card is fully visible. Skip cards cut off at the screen edge. Copy \
numbers exactly as shown and convert them: "1.17M" -> 1170000, "23K" -> 23000, "$125" -> 125. \
Use -1 for any number the screenshot doesn't show, and "" for missing text; never guess.

- campaign: the brand or creator name; bounty: the bounty's own name or slug ("" if none)
- status: active, disabled, ended, or unknown
- kind: "rate" (pays $X per N views) or "pot" (a fixed pot shared by view share)
- rate_usd and rate_per_views for rate bounties ("$125 / 100K views" -> 125 and 100000)
- pot_usd and min_views for pot bounties
- filled_pct: how much of the budget is used, 0-100, from the budget bar or its label
- clips and views: the totals shown for the bounty
- payout: paypal, crypto, other, or unknown (look for PayPal, USDT, USDC, ETH logos or text)
- category: podcast, interview, gaming, streamer, music, brand, or other, judged from the name, \
artwork and description
- notes: anything else that matters in under 15 words (language, region, platform limits)"""


NUMBERS = ("rate_usd", "rate_per_views", "pot_usd", "min_views", "filled_pct", "clips", "views")
INTS = ("rate_per_views", "min_views", "clips", "views")


class Bounty(BaseModel):
    # Every field required, no Optional: structured outputs reject schemas with many unions.
    # Numbers use -1 for "not shown"; clean() turns that into None.
    campaign: str
    bounty: str
    status: str
    kind: str
    rate_usd: float
    rate_per_views: float
    pot_usd: float
    min_views: float
    filled_pct: float
    clips: float
    views: float
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
    for k in ("status", "kind", "payout", "category"):
        d[k] = d[k].strip().lower()
    return d


class NotAnImage(ValueError):
    pass


def prepare_image(data: bytes, dest_dir: Path) -> tuple[str, Path]:
    """Shrink to MAX_EDGE and store as JPEG named by content hash. Returns (sha, path)."""
    sha = hashlib.sha256(data).hexdigest()[:16]
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{sha}.jpg"
    if out.exists():
        return sha, out
    with tempfile.TemporaryDirectory(dir=dest_dir) as tmp:
        src = Path(tmp) / "in"
        src.write_bytes(data)
        part = Path(tmp) / "out.jpg"
        proc = subprocess.run(
            [ffmpeg(), "-v", "error", "-y", "-i", str(src), "-frames:v", "1", "-vf",
             f"scale='if(gt(iw,ih),min({MAX_EDGE},iw),-2)':'if(gt(iw,ih),-2,min({MAX_EDGE},ih))'",
             "-q:v", "3", str(part)], capture_output=True, text=True)
        if proc.returncode or not part.exists():
            raise NotAnImage("not a readable image (use PNG or JPEG screenshots)")
        part.replace(out)  # atomic
    return sha, out


def read_screenshot(s: Settings, image_path: Path) -> tuple[list[dict], dict]:
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
        max_tokens=4000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": img}},
            {"type": "text", "text": PROMPT},
        ]}],
        output_format=ScoutRead,
    )
    tin, tout = resp.usage.input_tokens, resp.usage.output_tokens
    usd = llm_cost(s.select_model, tin, tout)
    meta = {"provider": "anthropic", "model": s.select_model, "tokens_in": tin,
            "tokens_out": tout, "usd": usd}
    return [clean(b) for b in resp.parsed_output.bounties], meta


def estimate_usd(s: Settings) -> float:
    return llm_cost(s.select_model, 2500, 1500) if s.select_provider == "anthropic" else 0.0


def _fake() -> list[dict]:
    """Two fixed bounties so the flow is testable offline."""
    common = {"kind": "rate", "status": "active", "rate_per_views": 100000, "pot_usd": -1,
              "min_views": -1, "notes": ""}
    return [
        clean(Bounty(campaign="Man On", bounty="episode-4", rate_usd=125, filled_pct=30,
                     clips=271, views=1170000, payout="paypal", category="podcast", **common)),
        clean(Bounty(campaign="Preme", bounty="preme", rate_usd=50, filled_pct=73, clips=86,
                     views=5830000, payout="crypto", category="music", **common)),
    ]
