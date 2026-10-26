"""Post caption builder: hook/title + campaign-required tags + niche hashtags + disclosure."""

NICHE_TAGS = {
    "gaming": ["#gaming", "#gamingclips", "#twitch"],
    "podcast": ["#podcast", "#podcastclips"],
    "interview": ["#interview", "#podcastclips"],
}
PLATFORM_REMINDER = {
    "tiktok": "Turn ON: Content disclosure → Paid partnership",
    "youtube": "Tick: Paid promotion (Details → Show more)",
    "instagram": "Turn ON: Paid partnership label",
    "x": "Keep #ad at the start of the post",
}


def build_caption(*, title: str, hook: str, category: str, required_tags: str,
                  caption_notes: str, disclosure: str) -> str:
    """Disclosure goes first: FTC guidance says it must be hard to miss, not buried in hashtags."""
    lead = (title or hook).strip()
    req = " ".join(t for t in required_tags.replace(",", " ").split() if t)
    niche = " ".join(NICHE_TAGS.get(category.lower().strip(), ["#clips"]))
    parts = [f"{disclosure.strip()} {lead}".strip() if disclosure.strip() else lead]
    if caption_notes.strip():
        parts.append(caption_notes.strip())
    tags = " ".join(dict.fromkeys(f"{req} {niche}".split()))  # dedupe, keep order
    if tags:
        parts.append(tags)
    return "\n\n".join(parts)
