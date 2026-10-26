"""API spend ledger and the monthly budget cap."""

import sqlite3
from datetime import UTC, datetime

from clipforge.config import get_settings

# USD per million tokens (input, output). Anthropic list prices as of 2026-10.
LLM_PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
# USD per audio hour.
STT_PRICES = {
    ("groq", "whisper-large-v3-turbo"): 0.04,
    ("groq", "whisper-large-v3"): 0.111,
    ("openai", "whisper-1"): 0.36,
}
CLI_ALIAS_TO_MODEL = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5",
                      "opus": "claude-opus-5-5"}


class BudgetExceeded(RuntimeError):
    pass


def llm_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    pin, pout = LLM_PRICES.get(model, LLM_PRICES["claude-sonnet-5-5"])
    return (tokens_in * pin + tokens_out * pout) / 1_000_000


def stt_cost(provider: str, model: str, audio_s: float) -> float:
    return STT_PRICES.get((provider, model), 0.0) * audio_s / 3600


def record(
    conn: sqlite3.Connection,
    *,
    provider: str,
    model: str,
    stage: str,
    units: str,
    usd: float,
    usd_equiv: float | None = None,
    source_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO api_costs (provider, model, stage, units, usd, usd_equiv, source_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (provider, model, stage, units, usd, usd if usd_equiv is None else usd_equiv, source_id),
    )


def month_spend(conn: sqlite3.Connection) -> float:
    start = datetime.now(UTC).strftime("%Y-%m-01T00:00:00Z")
    row = conn.execute(
        "SELECT COALESCE(SUM(usd),0) s FROM api_costs WHERE created_at >= ?", (start,)
    ).fetchone()
    return float(row["s"])


def day_spend(conn: sqlite3.Connection) -> float:
    start = datetime.now(UTC).strftime("%Y-%m-%dT00:00:00Z")
    row = conn.execute(
        "SELECT COALESCE(SUM(usd),0) s FROM api_costs WHERE created_at >= ?", (start,)
    ).fetchone()
    return float(row["s"])


def check_budget(conn: sqlite3.Connection, estimate_usd: float = 0.0) -> None:
    """Raise BudgetExceeded if this paid call would push the month over the cap."""
    cap = get_settings().monthly_budget_usd
    spent = month_spend(conn)
    if spent + estimate_usd > cap:
        raise BudgetExceeded(f"monthly budget ${cap:.2f} reached (spent ${spent:.2f})")


def budget_status(conn: sqlite3.Connection) -> dict:
    s = get_settings()
    spent = month_spend(conn)
    ratio = spent / s.monthly_budget_usd if s.monthly_budget_usd else 0
    return {
        "month": spent,
        "today": day_spend(conn),
        "cap": s.monthly_budget_usd,
        "ratio": ratio,
        "state": "paused" if ratio >= 1 else "warn" if ratio >= s.budget_warn_ratio else "ok",
    }
