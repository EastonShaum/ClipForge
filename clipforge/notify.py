"""Discord webhook notifications (an official Discord feature, not a self-bot)."""

import logging

import httpx

from clipforge.config import get_settings

log = logging.getLogger(__name__)


def discord(content: str) -> bool:
    url = get_settings().discord_webhook_url
    if not url:
        log.info("discord webhook not configured; would send: %s", content)
        return False
    try:
        r = httpx.post(url, json={"content": content[:1900], "allowed_mentions": {"parse": []}},
                       timeout=15)
        r.raise_for_status()
        return True
    except httpx.HTTPError as e:
        log.warning("discord webhook failed: %s", e)
        return False
