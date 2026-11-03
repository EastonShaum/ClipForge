"""Settings loaded from environment / .env. Every knob lives here so nothing is hard-coded."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_prefix="CF_", extra="ignore", env_file_encoding="utf-8"
    )

    # Storage
    data_dir: Path = ROOT / "data"
    # Campaign footage you downloaded, one folder per campaign (e.g. Campaigns/ManOn/...). The
    # laptop worker lists it for the Pi; files are clipped in place, never moved or deleted.
    media_library_dir: Path = ROOT / "Campaigns"
    media_cap_gb: float = 5.0  # hard cap for raw media on this host
    min_free_gb: float = 2.0  # refuse downloads below this much free disk
    raw_retention_hours: int = 48  # delete raw source media this long after its clips render
    # Live streams (D30). Recordings go here on the laptop, outside the media library so they
    # don't show up as footage; they're deleted live_keep_hours after the stream ends, once
    # nothing still needs them.
    live_dir: Path = ROOT / "Live"
    live_keep_hours: int = 72
    live_check_s: int = 120  # how often the Pi checks watched channels
    # Live clips are short and fast (D33): the top clipper of N3on's Halloween stream posted
    # 6-18 s clips 1-9 minutes after each moment. 5-min chunks get ours out in ~5-8 min.
    live_chunk_min: int = 5  # clip the recording in chunks this long
    live_clips_per_chunk: int = 2
    live_candidates_requested: int = 4
    live_min_clip_s: float = 6.0
    live_max_clip_s: float = 25.0
    # auto: IRL camera footage fills the screen (blurred bars make people tiny); a shared
    # screen keeps its full width (blurfill), since a center crop shows only its middle.
    live_layout: str = "auto"  # auto | crop | blurfill
    max_download_height: int = 1080

    # Host identity: "pi" for the orchestrator, anything else for a remote worker.
    host_name: str = "pi"

    # Web UI. Bind to the Tailscale IP in production; never 0.0.0.0.
    bind_host: str = "127.0.0.1"
    port: int = 8420
    public_base_url: str = "http://127.0.0.1:8420"  # used in Discord links
    worker_token: str = ""  # shared secret for remote workers calling the API

    # Providers
    transcribe_provider: str = "groq"  # groq | openai | fake | local
    groq_api_key: str = ""
    openai_api_key: str = ""
    transcribe_model: str = "whisper-large-v3-turbo"

    # anthropic (API key, default) | claude_cli (Pro subscription; ToS gray area, see DECISIONS)
    # | fake
    select_provider: str = "anthropic"
    anthropic_api_key: str = ""
    select_model: str = "claude-haiku-5-5"
    claude_cli_model: str = "haiku"  # alias passed to `claude -p --model`
    claude_cli_path: str = "claude"
    candidates_requested: int = 18
    candidates_keep: int = 6
    channel_list_max: int = 60  # D41: videos listed from a channel
    channel_min_video_s: float = 180.0  # D41: shorter uploads aren't offered or auto-queued
    min_clip_s: float = 15.0
    max_clip_s: float = 60.0

    # Render
    render_layout: str = "blurfill"  # blurfill | crop
    render_preset: str = "veryfast"
    render_crf: int = 21
    # Burned-in "Paid partnership #ad" label. Off by Easton's call (2026-10-29): he turns on the
    # platform's own paid-partnership toggle instead, and the post caption still starts with #ad.
    onscreen_disclosure: bool = False
    # Burned-in hook card. Off by Easton's call (2026-10-29): he adds the hook in TikTok's own
    # text tool on his iPhone (real Apple emojis), copied from the Post page.
    hook_card: bool = False
    emoji_font: str = ""  # color-emoji font for the hook card; empty = find the system one
    font_name: str = "Montserrat ExtraBold"
    fonts_dir: Path = ROOT / "assets" / "fonts"
    # The laptop counts as online if it polled the Pi within this many seconds.
    laptop_grace_s: int = 600
    # Unpinned job kinds the Pi leaves to the laptop while it is online (render jobs are always
    # pinned to whichever host holds the media, so only fetch matters).
    prefer_remote_for: list[str] = Field(default_factory=lambda: ["fetch"])

    # Budget
    monthly_budget_usd: float = 15.0
    budget_warn_ratio: float = 0.8

    # Notifications
    discord_webhook_url: str = ""

    # TikTok inbox uploads (D32): a sandbox app with only the video.upload scope.
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_redirect_uri: str = "https://raspberrypi.your-tailnet.ts.net:8443/auth/tiktok/callback"
    # Posting reminders, in local time. One slot = one post per platform per day (warm-up week).
    # Sprout Social 2026: TikTok's best window is Tue-Fri 2-6 pm (research/hooks-playbook.md §7).
    timezone: str = "America/Denver"
    post_slots: list[str] = Field(default_factory=lambda: ["17:00"])
    digest_time: str = "09:00"  # campaigns ending soon + links not yet submitted

    # Remote workers must run the exact commit the Pi runs (they self-update; see update.py).
    require_worker_version: bool = True

    # Policy
    blocked_categories: list[str] = Field(
        default_factory=lambda: ["gambling", "casino", "sports betting", "sportsbook", "poker",
                                 "prediction market"]
    )
    blocked_keywords: list[str] = Field(
        default_factory=lambda: ["stake", "casino", "roobet", "rollbit", "bet365", "draftkings",
                                 "fanduel", "gamdom", "shuffle.com", "slots", "jack.com", "blackjack",
                                 "roulette", "gambling", "poker", "sportsbook",
                                 # Prediction markets (D38): blocked by default; Easton decides.
                                 "polymarket", "kalshi", "prediction market"]
    )
    # Political campaigns are skipped unless the brief names the sponsor and its disclosure
    # terms (a box Easton ticks when adding one).
    terms_needed_categories: list[str] = Field(default_factory=lambda: ["political"])
    # Scout ranks these first (D38): they fit the accounts and pay off on Content Rewards.
    priority_categories: list[str] = Field(
        default_factory=lambda: ["podcast", "interview", "comedy", "startup", "product", "soccer"])

    # Money (D38). Content Rewards' creator terms (§5, checked 2026-11-02): 10% of every CPM and
    # prize-pool payout; per-post and retainer: 10% under a $5,000 budget, 0% at $5,000 or more.
    # Payouts recorded on posts show the real fee; change these if they disagree.
    cr_fee: float = 0.10
    cr_fee_free_budget_usd: float = 5000.0
    target_hourly_usd: float = 50.0  # Easton's freelance rate: the bar for $/hour
    # Chance a post reaches at least N views, until an account has min_posts_for_odds posts
    # with views: then its own history replaces these. TikTok's median post gets ~500 views.
    view_odds: dict[int, float] = Field(default_factory=lambda: {
        500: 0.5, 10_000: 0.30, 100_000: 0.03, 1_000_000: 0.003})
    min_posts_for_odds: int = 30
    max_views: int = 10_000_000  # no clip is counted on past this (keeps uncapped EV finite)
    budget_low_ratio: float = 0.2  # ping when a bounty's budget left drops below this
    near_min_ratio: float = 0.7  # ping when a post reaches this share of its minimum views
    end_soon_days: int = 21  # Scout marks down bounties ending sooner than this

    # Posting cadence (D38): one post a day per platform while an account warms up, then 1-2.
    warmup_days: int = 14
    posts_per_day_max: int = 2

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipforge.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def assets_dir(self) -> Path:
        """Campaign assets (logos). The Pi holds the originals; remote workers cache copies."""
        return self.data_dir / "assets"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for d in (s.data_dir, s.raw_dir, s.clips_dir, s.work_dir, s.assets_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
