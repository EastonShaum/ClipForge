-- ClipForge schema. Applied idempotently on startup (CREATE ... IF NOT EXISTS).
-- Times are ISO-8601 UTC strings; money is USD as REAL.

CREATE TABLE IF NOT EXISTS campaigns (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    platform        TEXT NOT NULL DEFAULT 'clipping.net',   -- the clipping marketplace
    category        TEXT NOT NULL DEFAULT '',               -- gaming / podcast / interview / ...
    rate_usd        REAL NOT NULL DEFAULT 0,                -- pay per rate_per_views views
    rate_per_views  INTEGER NOT NULL DEFAULT 1000,          -- e.g. 100000 for "$40 per 100K"
    min_views       INTEGER NOT NULL DEFAULT 0,             -- per-clip threshold to qualify
    cap_usd         REAL,                                   -- per-clip payout cap
    allowed_platforms TEXT NOT NULL DEFAULT 'tiktok,youtube',
    required_tags   TEXT NOT NULL DEFAULT '',               -- tags/mentions that must be in caption
    caption_notes   TEXT NOT NULL DEFAULT '',               -- extra required caption text
    disclosure      TEXT NOT NULL DEFAULT '#ad',
    source_links    TEXT NOT NULL DEFAULT '',               -- campaign drive/channel links
    watch_feeds     TEXT NOT NULL DEFAULT '',               -- YouTube RSS URLs to poll
    end_date        TEXT,
    notes           TEXT NOT NULL DEFAULT '',
    logo_path       TEXT,                                   -- assets/-relative PNG burned into clips
    status          TEXT NOT NULL DEFAULT 'active',         -- active | paused | ended | blocked
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY,
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id),
    url             TEXT NOT NULL,
    source_key      TEXT NOT NULL UNIQUE,                   -- e.g. youtube:dQw4w9WgXcQ (dedupe)
    title           TEXT NOT NULL DEFAULT '',
    duration_s      REAL,
    media_host      TEXT,                                   -- host that holds the raw media
    media_path      TEXT,                                   -- path on media_host
    status          TEXT NOT NULL DEFAULT 'new',            -- new|fetched|transcribed|selected|rendered|failed
    error           TEXT,
    raw_deleted_at  TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS transcripts (
    id              INTEGER PRIMARY KEY,
    source_id       INTEGER NOT NULL UNIQUE REFERENCES sources(id),
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    language        TEXT,
    words_json      TEXT NOT NULL,                          -- [{"w":..,"s":ms,"e":ms}, ...]
    text            TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,                   -- e.g. select-v1
    body_sha        TEXT NOT NULL,
    body            TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    weight          REAL NOT NULL DEFAULT 1.0,              -- A/B traffic weight
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS candidates (
    id              INTEGER PRIMARY KEY,
    source_id       INTEGER NOT NULL REFERENCES sources(id),
    start_ms        INTEGER NOT NULL,
    end_ms          INTEGER NOT NULL,
    hook_text       TEXT NOT NULL,
    hook_type       TEXT NOT NULL DEFAULT 'other',
    title           TEXT NOT NULL DEFAULT '',
    rationale       TEXT NOT NULL DEFAULT '',
    score           REAL NOT NULL,
    rank            INTEGER NOT NULL,
    kept            INTEGER NOT NULL DEFAULT 0,             -- in top-N → gets rendered
    prompt_version_id INTEGER REFERENCES prompt_versions(id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (source_id, start_ms, end_ms)
);

CREATE TABLE IF NOT EXISTS clips (
    id              INTEGER PRIMARY KEY,
    candidate_id    INTEGER NOT NULL REFERENCES candidates(id),
    preset          TEXT NOT NULL DEFAULT 'vertical',
    path            TEXT NOT NULL,                          -- relative to clips_dir on the Pi
    size_bytes      INTEGER,
    render_s        REAL,                                   -- wall time to render
    rendered_by     TEXT,
    status          TEXT NOT NULL DEFAULT 'in_review',      -- in_review|approved|rejected|posted
    caption         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (candidate_id, preset)
);

CREATE TABLE IF NOT EXISTS reviews (
    id              INTEGER PRIMARY KEY,
    clip_id         INTEGER NOT NULL REFERENCES clips(id),
    decision        TEXT NOT NULL,                          -- approved | rejected
    reason          TEXT,                                   -- picker value for rejections
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY,
    clip_id         INTEGER NOT NULL REFERENCES clips(id),
    platform        TEXT NOT NULL,                          -- tiktok | youtube | instagram | x
    url             TEXT NOT NULL UNIQUE,
    posted_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    submitted_at    TEXT                                    -- link handed to the campaign
);

CREATE TABLE IF NOT EXISTS view_snapshots (
    id              INTEGER PRIMARY KEY,
    post_id         INTEGER NOT NULL REFERENCES posts(id),
    views           INTEGER NOT NULL,
    likes           INTEGER,
    source          TEXT NOT NULL DEFAULT 'manual',         -- manual | api | campaign
    taken_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS api_costs (
    id              INTEGER PRIMARY KEY,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    stage           TEXT NOT NULL,
    units           TEXT NOT NULL DEFAULT '',               -- "3600 audio s" / "21000 in, 3800 out"
    usd             REAL NOT NULL DEFAULT 0,                -- actually billed
    usd_equiv       REAL NOT NULL DEFAULT 0,                -- what it would cost on the paid API
    source_id       INTEGER,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Work queue. One row per unit of work; dedupe_key makes enqueueing idempotent.
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,                          -- fetch|transcribe|select|render
    dedupe_key      TEXT NOT NULL UNIQUE,
    source_id       INTEGER,
    candidate_id    INTEGER,
    state           TEXT NOT NULL DEFAULT 'queued',         -- queued|running|done|failed|dead
    affinity_host   TEXT,                                   -- only this host may claim it
    priority        INTEGER NOT NULL DEFAULT 100,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 5,
    run_after       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    locked_by       TEXT,
    locked_until    TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(state, kind, run_after);

CREATE TABLE IF NOT EXISTS workers (
    name            TEXT PRIMARY KEY,
    kinds           TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_kv (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);

-- Campaign scouting: marketplace screenshots you upload, and the bounties read off them.
CREATE TABLE IF NOT EXISTS scout_shots (
    id              INTEGER PRIMARY KEY,
    sha             TEXT NOT NULL UNIQUE,                   -- same image twice = one row
    path            TEXT NOT NULL,                          -- relative to assets_dir
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS scout_bounties (
    id              INTEGER PRIMARY KEY,
    shot_id         INTEGER NOT NULL REFERENCES scout_shots(id),
    key             TEXT NOT NULL,                          -- normalized campaign|bounty
    campaign        TEXT NOT NULL,
    bounty          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'unknown',
    kind            TEXT NOT NULL DEFAULT 'rate',           -- rate | pot
    rate_usd        REAL,
    rate_per_views  INTEGER,
    pot_usd         REAL,
    min_views       INTEGER,
    filled_pct      REAL,
    clips           INTEGER,
    views           INTEGER,
    payout          TEXT NOT NULL DEFAULT 'unknown',
    category        TEXT NOT NULL DEFAULT 'other',
    notes           TEXT NOT NULL DEFAULT '',
    seen_at         TEXT NOT NULL                           -- when the screenshot was uploaded
);
CREATE INDEX IF NOT EXISTS scout_bounties_key ON scout_bounties (key, seen_at);

-- Screenshots of a campaign's brief / requirements doc, and what was read off them.
CREATE TABLE IF NOT EXISTS campaign_docs (
    id              INTEGER PRIMARY KEY,
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id),
    sha             TEXT NOT NULL,
    path            TEXT NOT NULL,                          -- relative to assets_dir
    extracted       TEXT,                                   -- JSON from stages/brief.py
    applied_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (campaign_id, sha)
);

-- Media files a worker found in its media_library_dir (one folder per campaign). Replaced
-- wholesale on every report from that host.
CREATE TABLE IF NOT EXISTS library_files (
    host            TEXT NOT NULL,
    path            TEXT NOT NULL,                          -- absolute path on that host
    rel             TEXT NOT NULL,                          -- relative to the library, '/' separated
    folder          TEXT NOT NULL,                          -- first segment of rel ('' = top level)
    size            INTEGER NOT NULL,
    mtime           REAL NOT NULL,                          -- unix seconds
    duration_s      REAL,
    seen_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (host, path)
);
