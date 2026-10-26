"""`clipforge` command line. Run `uv run clipforge --help`."""

import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Annotated

import typer

from clipforge.config import get_settings

app = typer.Typer(no_args_is_help=True, add_completion=False)
campaign_app = typer.Typer(no_args_is_help=True, help="Manage campaigns")
app.add_typer(campaign_app, name="campaign")


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _init():
    from clipforge.db import connect, init_db
    from clipforge.select_prompts import sync_prompts

    init_db()
    conn = connect()
    sync_prompts(conn)
    return conn


@app.command()
def init():
    """Create the database and register prompt versions."""
    conn = _init()
    s = get_settings()
    typer.echo(f"Database ready at {s.db_path}")
    for r in conn.execute("SELECT name, active FROM prompt_versions"):
        typer.echo(f"  prompt {r['name']} {'(active)' if r['active'] else ''}")


@campaign_app.command("add")
def campaign_add(
    name: str,
    rate: Annotated[float, typer.Option(help="Pay in USD per --per views")] = 0.0,
    per: Annotated[int, typer.Option(help="Views the rate applies to (1000, 100000…)")] = 1000,
    category: str = "podcast",
    min_views: int = 0,
    cap: Annotated[float | None, typer.Option(help="Per-clip payout cap USD")] = None,
    platforms: str = "tiktok,youtube",
    tags: str = "",
    disclosure: str = "#ad",
    feeds: Annotated[str, typer.Option(help="Space-separated YouTube RSS URLs")] = "",
    marketplace: str = "clipping.net",
    notes: str = "",
    force: bool = False,
):
    """Add a campaign. Example: clipforge campaign add "Pod X" --rate 40 --per 100000"""
    from clipforge.campaigns import CampaignIn, add_campaign, effective_per_1k

    conn = _init()
    c = CampaignIn(name=name, rate_usd=rate, rate_per_views=per, category=category,
                   min_views=min_views, cap_usd=cap, allowed_platforms=platforms,
                   required_tags=tags, disclosure=disclosure, watch_feeds=feeds,
                   platform=marketplace, notes=notes)
    try:
        cid = add_campaign(conn, c, force=force)
    except ValueError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(1) from e
    typer.echo(f"Campaign #{cid} added: ${effective_per_1k(rate, per):.2f} per 1K views")


@campaign_app.command("logo")
def campaign_logo(campaign_id: int, image: Path):
    """Set the logo a campaign requires in every clip (PNG with transparency works best)."""
    from clipforge.campaigns import set_logo

    rel = set_logo(_init(), campaign_id, image.read_bytes())
    typer.echo(f"campaign #{campaign_id} logo -> {rel}")


@campaign_app.command("list")
def campaign_list():
    from clipforge.campaigns import list_campaigns

    for c in list_campaigns(_init()):
        typer.echo(f"#{c['id']:<3} {c['status']:<7} ${c['per_1k']:.2f}/1K  min {c['min_views']:>7,}"
                   f"  {c['name']}")


@app.command()
def source(
    campaign_id: int,
    urls: list[str],
    on: Annotated[str | None, typer.Option(help="Machine that has these local files "
                                           "(default: C:\\… = laptop, /… = this Pi)")] = None,
):
    """Queue footage for a campaign: links, or paths of files already downloaded."""
    from clipforge.db import tx
    from clipforge.ingest import add_source

    conn = _init()
    for url in urls:
        with tx(conn):
            sid, created = add_source(conn, campaign_id, url, on)
        typer.echo(f"{'queued' if created else 'already known'}: source #{sid} {url}")


@app.command()
def status():
    """Queue and budget overview."""
    from clipforge import costs, jobqueue

    conn = _init()
    typer.echo(json.dumps({"jobs": jobqueue.counts(conn), "budget": costs.budget_status(conn)},
                          indent=2))
    for r in conn.execute("SELECT id, kind, state, attempts, last_error FROM jobs WHERE state IN"
                          " ('queued','dead') AND last_error IS NOT NULL ORDER BY id DESC LIMIT 10"):
        typer.echo(f"job {r['id']} {r['kind']} {r['state']} x{r['attempts']}: {r['last_error']}")


@app.command()
def serve(host: str | None = None, port: int | None = None):
    """Run the review UI + API."""
    import uvicorn

    _setup_logging()
    s = get_settings()
    uvicorn.run("clipforge.web.app:create_app", factory=True, host=host or s.bind_host,
                port=port or s.port, proxy_headers=True, log_level="info")


@app.command()
def worker(
    kinds: Annotated[str, typer.Option(help="Comma list of fetch,transcribe,select,render")] =
    "fetch,transcribe,select,render",
    label: str = "main",
    once: Annotated[bool, typer.Option(help="Exit when the queue is empty")] = False,
    verbose: bool = False,
):
    """Run a local worker on this machine (the Pi)."""
    from clipforge.worker import run_local

    _setup_logging(verbose)
    _init().close()
    run_local([k.strip() for k in kinds.split(",") if k.strip()], label=label, once=once)


@app.command("remote-worker")
def remote_worker(
    pi_url: Annotated[str, typer.Option(help="e.g. http://raspberrypi:8420")],
    kinds: str = "fetch,transcribe,render",
):
    """Run a worker on another machine (the laptop) that pulls jobs from the Pi."""
    from clipforge.worker import run_remote

    _setup_logging()
    run_remote(pi_url, [k.strip() for k in kinds.split(",") if k.strip()])


@app.command()
def scheduler():
    """Run periodic tasks: feed polling, raw-media cleanup, budget alerts."""
    from clipforge import scheduler as sched

    _setup_logging()
    _init().close()
    sched.run()


@app.command()
def cleanup():
    """Delete expired raw media now."""
    from clipforge.scheduler import cleanup_once

    _init().close()
    typer.echo(f"freed {cleanup_once() / 1e9:.2f} GB")


@app.command()
def bench(
    seconds: float = 45.0,
    layout: str = "blurfill",
    preset: Annotated[str | None, typer.Option(help="x264 preset override")] = None,
):
    """Benchmark rendering a captioned 1080x1920 clip from a synthetic 1080p source."""
    from clipforge.stages.render import render
    from clipforge.stages.transcribe import fake_transcript

    s = get_settings()
    if preset:
        s = s.model_copy(update={"render_preset": preset})
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        src = tmpd / "src.mp4"
        typer.echo("making a synthetic 1080p30 source…")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
             f"testsrc2=size=1920x1080:rate=30:duration={seconds + 2}",
             "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds + 2}",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src)],
            check=True)
        words = fake_transcript(seconds + 2)["words"]
        t0 = time.monotonic()
        out = render(s, media_path=str(src), words=words, start_ms=1000,
                     end_ms=int((seconds + 1) * 1000), hook="This changes everything",
                     out_path=tmpd / "out.mp4", work=tmpd / "w", layout=layout,
                     disclosure="Paid partnership #ad")
        wall = time.monotonic() - t0
    typer.echo(json.dumps({"clip_s": seconds, "layout": layout, "preset": s.render_preset,
                           "render_s": round(wall, 1), "realtime_x": round(seconds / wall, 2),
                           "size_mb": round(out["size_bytes"] / 1e6, 1)}))


if __name__ == "__main__":
    app()
