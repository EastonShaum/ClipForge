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
    live_chunk_min: int = 30  # clip the recording in chunks this long
    live_clips_per_chunk: int = 3
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
    # Posting reminders, in local time. One slot = one post per platform per day (warm-up week).
    # Sprout Social 2026: TikTok's best window is Tue-Fri 2-6 pm (research/hooks-playbook.md §7).
    timezone: str = "America/Denver"
    post_slots: list[str] = Field(default_factory=lambda: ["17:00"])
    digest_time: str = "09:00"  # campaigns ending soon + links not yet submitted

    # Remote workers must run the exact commit the Pi runs (they self-update; see update.py).
    require_worker_version: bool = True

    # Policy
    blocked_categories: list[str] = Field(
        default_factory=lambda: ["gambling", "casino", "sports betting", "sportsbook"]
    )
    blocked_keywords: list[str] = Field(
        default_factory=lambda: ["stake", "casino", "roobet", "rollbit", "bet365", "draftkings",
                                 "fanduel", "gamdom", "shuffle.com", "slots"]
    )

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
