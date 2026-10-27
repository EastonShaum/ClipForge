"""Bounty brief screenshots: store what the brief stage read, and apply it to the bounty (and
its campaign's platforms) when you say so. Applying only adds or fills in; it never removes what
you entered, and it never changes the disclosure (our #ad + on-screen tag stays)."""

import json
import re
import sqlite3

PLATFORMS = ("tiktok", "youtube", "instagram", "x")


def _tag(t: str) -> str:
    t = t.strip()
    return t if t[:1] in "#@" else f"#{t}"


def merged(conn: sqlite3.Connection, bounty_id: int) -> dict:
    """Everything read from this bounty's brief screenshots, deduplicated, in upload order."""
    out = {"summary": [], "required_tags": [], "caption_text": [], "disclosure": [],
           "platforms": [], "end_date": [], "footage_links": [], "logo_rule": [], "rules": [],
           "length": []}
    rows = conn.execute("SELECT extracted FROM campaign_docs WHERE bounty_id=? AND extracted"
                        " IS NOT NULL ORDER BY id", (bounty_id,)).fetchall()
    for r in rows:
        b = json.loads(r["extracted"])
        for k in ("summary", "caption_text", "disclosure", "end_date", "logo_rule"):
            if b.get(k):
                out[k].append(b[k].strip())
        out["required_tags"] += [_tag(t) for t in b.get("required_tags", []) if t.strip()]
        out["platforms"] += [p.lower() for p in b.get("platforms", []) if p.lower() in PLATFORMS]
        out["footage_links"] += b.get("footage_links", [])
        out["rules"] += b.get("rules", [])
        lo, hi = b.get("min_seconds", -1), b.get("max_seconds", -1)
        if lo > 0 or hi > 0:
            out["length"].append(f"{lo:.0f}" if lo > 0 else "?")
            out["length"][-1] += f"–{hi:.0f} s" if hi > 0 else "+ s"
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}


def apply(conn: sqlite3.Connection, bounty_id: int) -> list[str]:
    """Fill the bounty from its briefs. Returns what changed, for the confirmation message."""
    b = conn.execute("SELECT * FROM bounties WHERE id=?", (bounty_id,)).fetchone()
    c = conn.execute("SELECT * FROM campaigns WHERE id=?", (b["campaign_id"],)).fetchone()
    m = merged(conn, bounty_id)
    changes, sets, camp_sets = [], {}, {}

    have = f"{c['required_tags']} {b['required_tags']}".split()
    new = [t for t in m["required_tags"] if t.lower() not in {h.lower() for h in have}]
    if new:
        sets["required_tags"] = " ".join([*b["required_tags"].split(), *new])
        changes.append(f"tags: {' '.join(new)}")
    if m["platforms"]:
        plats = ",".join(p for p in PLATFORMS if p in m["platforms"])
        if plats != c["allowed_platforms"]:
            camp_sets["allowed_platforms"] = plats
            changes.append(f"platforms: {plats}")
    if m["end_date"] and re.fullmatch(r"\d{4}-\d{2}-\d{2}", m["end_date"][-1]) \
            and m["end_date"][-1] != b["end_date"]:
        sets["end_date"] = m["end_date"][-1]
        changes.append(f"end date: {sets['end_date']}")
    links = [u for u in m["footage_links"] if u not in b["source_links"]]
    if links:
        sets["source_links"] = " ".join(filter(None, [b["source_links"], *links]))
        changes.append(f"{len(links)} footage link(s)")
    notes = [n for n in ([f"Caption must include: {t}" for t in m["caption_text"]]
                         + m["logo_rule"] + [f"Length {x}" for x in m["length"]] + m["rules"])
             if n not in b["notes"]]
    if notes:
        sets["notes"] = " · ".join(filter(None, [b["notes"], *notes]))
        changes.append(f"{len(notes)} rule(s) in notes")
    for table, row_id, vals in (("bounties", bounty_id, sets),
                                ("campaigns", c["id"], camp_sets)):
        if vals:
            cols = ", ".join(f"{k}=?" for k in vals)
            conn.execute(f"UPDATE {table} SET {cols} WHERE id=?", (*vals.values(), row_id))
    if changes:
        conn.execute("UPDATE campaign_docs SET applied_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                     " WHERE bounty_id=? AND extracted IS NOT NULL", (bounty_id,))
    return changes
