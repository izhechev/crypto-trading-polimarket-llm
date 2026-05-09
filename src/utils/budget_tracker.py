"""
LLM call logger and daily budget enforcer.

Tracks every Groq (and future Claude) API call in a lightweight SQLite DB.
Enforces hard daily spending caps defined in config.DAILY_BUDGET_LIMITS.

Usage:
    from src.utils.budget_tracker import log_llm_call, check_budget, get_daily_stats

    # Before making an LLM call:
    check_budget("groq")   # raises BudgetExceededError if at limit

    # After the call completes:
    log_llm_call(model="groq", tokens_in=800, tokens_out=300, endpoint="analyze")
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config


# ── DB setup ──────────────────────────────────────────────────────────────

DB_PATH = config.DATA_DIR / "llm_calls.db"


@contextmanager
def _db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _ensure_table() -> None:
    try:
        with _db() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS llm_calls (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          TEXT    NOT NULL,
                    model       TEXT    NOT NULL,
                    tokens_in   INTEGER NOT NULL DEFAULT 0,
                    tokens_out  INTEGER NOT NULL DEFAULT 0,
                    cost_usd    REAL    NOT NULL DEFAULT 0.0,
                    endpoint    TEXT    NOT NULL DEFAULT ''
                )
            """)
    except Exception:
        pass  # DB may be locked — will retry on next call


_ensure_table()


# ── Cost calculation ──────────────────────────────────────────────────────

# Groq free tier: $0 per token — but track calls/tokens for rate limit awareness.
# Add Claude pricing here when migrating.
_COST_PER_1M = {
    "groq":           {"in": 0.00,  "out": 0.00},   # Free tier
    "haiku":          {"in": 1.00,  "out": 5.00},   # Claude Haiku 4.5
    "sonnet":         {"in": 3.00,  "out": 15.00},  # Claude Sonnet 4.6
    "opus":           {"in": 15.00, "out": 75.00},  # Claude Opus 4.6
    "cohere_embed":   {"in": 0.12,  "out": 0.00},
    "tavily":         {"in": 0.00,  "out": 0.00},   # Tracked by calls (credits), not tokens
}

# Tavily free tier: 1,000 credits/month. Each basic search = 1 credit.
_TAVILY_MONTHLY_FREE = 1_000


def _calc_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    rates = _COST_PER_1M.get(model.lower(), {"in": 0.0, "out": 0.0})
    return (tokens_in * rates["in"] + tokens_out * rates["out"]) / 1_000_000


# ── Public API ────────────────────────────────────────────────────────────

class BudgetExceededError(Exception):
    """Raised when a daily budget limit would be exceeded."""
    pass


def log_llm_call(
    model: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    endpoint: str = "",
) -> float:
    """
    Log an LLM API call to the SQLite DB.
    Returns the cost in USD (0.0 for free-tier models like Groq).
    """
    cost = _calc_cost(model, tokens_in, tokens_out)
    ts = datetime.now(timezone.utc).isoformat()
    with _db() as con:
        con.execute(
            "INSERT INTO llm_calls (ts, model, tokens_in, tokens_out, cost_usd, endpoint) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, model.lower(), tokens_in, tokens_out, cost, endpoint),
        )
    return cost


def get_daily_stats(model: str | None = None) -> dict:
    """
    Return today's usage stats.
    If model is given, filter to that model only.
    Returns {calls, tokens_in, tokens_out, cost_usd}.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _db() as con:
        if model:
            row = con.execute(
                "SELECT COUNT(*) as calls, "
                "       SUM(tokens_in) as tokens_in, "
                "       SUM(tokens_out) as tokens_out, "
                "       SUM(cost_usd) as cost_usd "
                "FROM llm_calls WHERE date(ts) = ? AND model = ?",
                (today, model.lower()),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT COUNT(*) as calls, "
                "       SUM(tokens_in) as tokens_in, "
                "       SUM(tokens_out) as tokens_out, "
                "       SUM(cost_usd) as cost_usd "
                "FROM llm_calls WHERE date(ts) = ?",
                (today,),
            ).fetchone()
    return {
        "calls":      row["calls"] or 0,
        "tokens_in":  row["tokens_in"] or 0,
        "tokens_out": row["tokens_out"] or 0,
        "cost_usd":   row["cost_usd"] or 0.0,
    }


def check_budget(model: str) -> None:
    """
    Check whether an LLM call is permitted under today's budget.
    Raises BudgetExceededError with a descriptive message if the limit is hit.
    Sends a Telegram warning at 80% of budget (non-blocking).
    """
    limits = config.DAILY_BUDGET_LIMITS.get(model.lower())
    if not limits:
        return  # No limit configured → allow

    stats = get_daily_stats(model)
    calls     = stats["calls"]
    tokens    = stats["tokens_in"] + stats["tokens_out"]
    cost      = stats["cost_usd"]

    max_calls  = limits.get("max_calls")
    max_tokens = limits.get("max_tokens")
    max_cost   = limits.get("max_cost_usd")

    # Hard stops
    if max_calls and calls >= max_calls:
        raise BudgetExceededError(
            f"[{model}] Daily call limit reached: {calls}/{max_calls} calls. "
            "HARD STOP — no more LLM calls until midnight UTC."
        )
    if max_tokens and tokens >= max_tokens:
        raise BudgetExceededError(
            f"[{model}] Daily token limit reached: {tokens:,}/{max_tokens:,} tokens. "
            "HARD STOP."
        )
    if max_cost and cost >= max_cost:
        raise BudgetExceededError(
            f"[{model}] Daily cost limit reached: ${cost:.4f}/${max_cost:.2f}. "
            "HARD STOP."
        )

    # 80% warning (non-blocking)
    try:
        warn_triggered = False
        if max_calls and calls >= max_calls * 0.8:
            warn_triggered = True
            _telegram_warn(
                f"⚠️ [{model}] LLM budget at {calls/max_calls:.0%}: "
                f"{calls}/{max_calls} calls today"
            )
        if max_cost and cost >= max_cost * 0.8 and not warn_triggered:
            _telegram_warn(
                f"⚠️ [{model}] LLM budget at {cost/max_cost:.0%}: "
                f"${cost:.4f}/${max_cost:.2f} today"
            )
    except Exception:
        pass  # Never let warning failure block the actual call


def _telegram_warn(message: str) -> None:
    try:
        from src.utils.telegram import send_telegram
        send_telegram(message)
    except Exception:
        pass


def print_daily_summary() -> None:
    """Print today's LLM usage summary to console."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _db() as con:
        rows = con.execute(
            "SELECT model, COUNT(*) as calls, SUM(tokens_in) as ti, "
            "       SUM(tokens_out) as to_, SUM(cost_usd) as cost "
            "FROM llm_calls WHERE date(ts) = ? GROUP BY model",
            (today,),
        ).fetchall()

    if not rows:
        print(f"  LLM calls today: 0")
        return

    print(f"\n  LLM Usage — {today}")
    print(f"  {'─'*50}")
    total_cost = 0.0
    for r in rows:
        model = r["model"]
        limits = config.DAILY_BUDGET_LIMITS.get(model, {})
        max_calls = limits.get("max_calls", "∞")
        cost = r["cost"] or 0.0
        total_cost += cost
        tokens_total = (r["ti"] or 0) + (r["to_"] or 0)
        print(
            f"  {model:12s}  {r['calls']:>4} calls "
            f"({r['calls']}/{max_calls})  "
            f"{tokens_total:>8,} tokens  ${cost:.4f}"
        )
    print(f"  {'─'*50}")
    # print(f"  Total cost today: ${total_cost:.4f}")


def get_tavily_monthly_usage() -> dict:
    """
    Return Tavily usage for the current calendar month.
    Each logged Tavily call = 1 credit used.
    Returns {calls_this_month, credits_remaining, pct_used, days_left}.
    """
    from calendar import monthrange
    now   = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    _, days_in_month = monthrange(now.year, now.month)
    days_left = days_in_month - now.day

    with _db() as con:
        row = con.execute(
            "SELECT COUNT(*) as calls FROM llm_calls "
            "WHERE strftime('%Y-%m', ts) = ? AND model = 'tavily'",
            (month,),
        ).fetchone()

    calls_used = row["calls"] if row else 0
    remaining  = max(0, _TAVILY_MONTHLY_FREE - calls_used)
    pct        = calls_used / _TAVILY_MONTHLY_FREE * 100

    return {
        "calls_this_month": calls_used,
        "credits_remaining": remaining,
        "pct_used":         pct,
        "days_left":        days_left,
        "monthly_cap":      _TAVILY_MONTHLY_FREE,
    }


def print_tavily_status() -> None:
    """Print Tavily monthly credit usage to console."""
    s = get_tavily_monthly_usage()
    bar_filled = int(s["pct_used"] / 5)          # 20-char bar
    bar        = "█" * bar_filled + "░" * (20 - bar_filled)
    icon       = "🟢" if s["pct_used"] < 70 else ("🟡" if s["pct_used"] < 90 else "🔴")
    print(f"\n  {icon}  Tavily AI — Monthly Credits")
    print(f"  {'─'*40}")
    print(f"  Used:      {s['calls_this_month']:>4} / {s['monthly_cap']} credits  ({s['pct_used']:.1f}%)")
    print(f"  Remaining: {s['credits_remaining']:>4} credits")
    print(f"  Days left: {s['days_left']} days in month")
    print(f"  [{bar}]")
    if s["credits_remaining"] < 100:
        print(f"  ⚠️  Low credits — consider upgrading at app.tavily.com")
