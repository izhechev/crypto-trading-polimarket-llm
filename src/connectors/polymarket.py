"""
Polymarket connector — prediction market odds (short-term focus).
No API key required. Uses the public Gamma API.

Short-term filter: only markets resolving within 7 days are returned
to `fetch_top_markets`. Long-term markets (elections, championships, etc.)
are skipped — they never resolve so track records stay at 0 wins.
"""
import json
import time
from datetime import datetime, timezone, timedelta
import httpx
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_BASE = "https://gamma-api.polymarket.com"
_cache: dict = {}
_CACHE_TTL = 1800  # 30 min
_SHORT_TERM_DAYS = 7  # only markets resolving within this window


def _cached(key):
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return data
    return None


def _set_cache(key, data):
    _cache[key] = (time.time(), data)


def _parse_probability(outcome_prices) -> float | None:
    """Parse first outcome probability from various formats Polymarket returns."""
    if outcome_prices is None:
        return None
    try:
        if isinstance(outcome_prices, list) and outcome_prices:
            return float(outcome_prices[0])
        if isinstance(outcome_prices, str):
            parsed = json.loads(outcome_prices)
            if parsed:
                return float(parsed[0])
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def _parse_end_date(raw: str | None) -> datetime | None:
    """Parse Polymarket endDate string → UTC datetime, or None if missing/unparseable."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:26], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _days_until(end_dt: datetime | None) -> float | None:
    """Return fractional days until end_dt, or None if unknown."""
    if end_dt is None:
        return None
    delta = end_dt - datetime.now(timezone.utc)
    return delta.total_seconds() / 86400


def _fetch_markets(cache_key: str, limit: int, tag_slug: str | None = None) -> list[dict]:
    """Generic market fetcher — crypto tag or all categories by volume."""
    cached = _cached(cache_key)
    if cached is not None:
        return cached
    params: dict = {
        "active":    "true",
        "closed":    "false",
        "limit":     limit,
        "order":     "volume",
        "ascending": "false",
    }
    if tag_slug:
        params["tag_slug"] = tag_slug
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{_BASE}/markets", params=params)
        if resp.status_code != 200:
            _set_cache(cache_key, [])
            return []
        markets = resp.json() or []
        result = []
        for m in markets[:limit]:
            # ── Outcome names & multi-outcome detection ──────────────────
            raw_outcomes = m.get("outcomes") or []
            if isinstance(raw_outcomes, str):
                try:
                    raw_outcomes = json.loads(raw_outcomes)
                except Exception:
                    raw_outcomes = []
            outcomes = [str(o) for o in raw_outcomes]
            # Binary: exactly 2 outcomes where first is "Yes"/"No" variant
            is_binary = (
                len(outcomes) == 2
                and outcomes[0].lower() in ("yes", "y", "true")
            )
            is_multi_outcome = bool(outcomes) and not is_binary

            prob = _parse_probability(m.get("outcomePrices"))
            # For multi-outcome, build a list of (name, prob) pairs
            outcome_probs: list[tuple[str, float]] = []
            if is_multi_outcome and outcomes:
                raw_prices = m.get("outcomePrices") or []
                if isinstance(raw_prices, str):
                    try:
                        raw_prices = json.loads(raw_prices)
                    except Exception:
                        raw_prices = []
                for name, price in zip(outcomes, raw_prices):
                    try:
                        outcome_probs.append((name, round(float(price) * 100, 1)))
                    except (ValueError, TypeError):
                        pass

            volume = 0.0
            try:
                volume = float(m.get("volumeNum") or m.get("volume") or 0)
            except (ValueError, TypeError):
                pass
            end_dt    = _parse_end_date(m.get("endDate") or m.get("end_date"))
            days_left = _days_until(end_dt)

            # Extract event slug (used in URLs) from the events array; fall back to market slug
            event_slug = ""
            try:
                events = m.get("events", [])
                if isinstance(events, str):
                    import json as _json
                    events = _json.loads(events)
                if events:
                    event_slug = events[0].get("slug", "") or events[0].get("ticker", "")
            except Exception:
                pass
            if not event_slug:
                event_slug = m.get("slug", "")
            url = f"https://polymarket.com/event/{event_slug}" if event_slug else ""

            result.append({
                "question":        m.get("question", ""),
                "market_id":       str(m.get("id", "")),
                "probability":     prob,
                "volume_usd":      volume,
                "category":        m.get("category", ""),
                "end_date":        m.get("endDate") or m.get("end_date") or "",
                "days_left":       round(days_left, 1) if days_left is not None else None,
                "url":             url,
                "outcomes":        outcomes,           # e.g. ["$60K", "$80K"] or ["Yes", "No"]
                "outcome_probs":   outcome_probs,      # [(name, pct), ...] for multi-outcome
                "is_multi_outcome": is_multi_outcome,
            })
        _set_cache(cache_key, result)
        return result
    except Exception:
        _set_cache(cache_key, [])
        return []


def fetch_crypto_markets(limit: int = 10) -> list[dict]:
    """Fetch open, high-volume crypto prediction markets from Polymarket."""
    return _fetch_markets("poly_crypto", limit, tag_slug="crypto")


def fetch_top_markets(limit: int = 20) -> list[dict]:
    """
    Fetch top N SHORT-TERM markets (resolves within 7 days) across all categories.
    Fetches 5× the limit to ensure enough remain after the date filter.
    ⏰ Long-term markets (elections, championships, IPOs) are excluded automatically.
    """
    # Fetch a large batch — most top-volume markets are long-term, so we need extras
    raw = _fetch_markets("poly_all", limit * 5, tag_slug=None)

    # Filter to short-term only
    short_term = [
        m for m in raw
        if m.get("days_left") is not None and 0 < m["days_left"] <= _SHORT_TERM_DAYS
    ]

    # Filter out coinflip markets: <$100 volume OR near-50/50 odds (45–55%)
    # These are 5-min BNB/BTC price prediction markets — pure noise
    before_filter = len(short_term)
    short_term = [
        m for m in short_term
        if m.get("volume_usd", 0) >= 100
        and (m.get("probability") is None or not (0.45 <= m["probability"] <= 0.55))
    ]
    coinflip_skipped = before_filter - len(short_term)
    if coinflip_skipped:
        print(f"  Skipped {coinflip_skipped} coinflip/low-volume markets (<$100 vol or 45-55% odds)")

    # If no date info survived (API changed), fall back to all markets with a warning
    if not short_term:
        short_term = raw
        print(f"  ⚠️  Polymarket: no end_date in API response — showing all markets")
    else:
        skipped = len(raw) - len(short_term)
        if skipped:
            print(f"  ⏰ Short-term markets only (resolves <7 days) — filtered out {skipped} long-term")

    def _category_rank(m: dict) -> int:
        cat = (m.get("category") or "").lower()
        q   = (m.get("question") or "").lower()
        if "crypto" in cat or "bitcoin" in q or "ethereum" in q or "btc" in q:
            return 0
        if "sport" in cat or "nfl" in q or "nba" in q or "soccer" in q or "game" in q:
            return 1
        if "politi" in cat or "election" in q:
            return 2
        return 3

    short_term.sort(key=lambda m: (_category_rank(m), m.get("days_left", 99), -m.get("volume_usd", 0)))
    return short_term[:limit]


def check_market_resolution(question: str, market_id: str = "") -> dict | None:
    """
    Check if a market has resolved on Polymarket.
    Uses direct ID lookup (fast, accurate) if market_id provided,
    otherwise falls back to fetching recent closed markets by text match.

    Returns:
      Binary:       {'resolved': True, 'outcome': 'YES'/'NO', 'outcome_name': 'Yes'/'No'}
      Multi-outcome: {'resolved': True, 'outcome': '$80K', 'outcome_name': '$80K'}
      Not resolved: None
    """
    def _parse_outcome(m: dict) -> dict | None:
        if not m.get("closed"):
            return None
        prices = m.get("outcomePrices") or []
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except Exception:
                prices = []
        outcomes_raw = m.get("outcomes") or []
        if isinstance(outcomes_raw, str):
            try:
                outcomes_raw = json.loads(outcomes_raw)
            except Exception:
                outcomes_raw = []
        outcomes = [str(o) for o in outcomes_raw]
        is_binary = (
            len(outcomes) == 2
            and outcomes[0].lower() in ("yes", "y", "true")
        )

        if not prices:
            return None

        # Find the winning outcome (price closest to 1.0)
        try:
            float_prices = [float(p) for p in prices]
        except (ValueError, TypeError):
            return None

        max_idx   = max(range(len(float_prices)), key=lambda i: float_prices[i])
        max_price = float_prices[max_idx]

        if max_price < 0.9:
            return None   # not yet resolved (no dominant winner)

        if is_binary:
            outcome_str = "YES" if max_idx == 0 else "NO"
            name        = outcomes[max_idx] if max_idx < len(outcomes) else outcome_str
            return {"resolved": True, "outcome": outcome_str, "outcome_name": name, "winner_idx": max_idx}
        else:
            # Multi-outcome: return the winning outcome name
            name = outcomes[max_idx] if max_idx < len(outcomes) else str(max_idx)
            return {"resolved": True, "outcome": name, "outcome_name": name, "winner_idx": max_idx}


    try:
        with httpx.Client(timeout=15) as client:
            # Fast path: direct ID lookup
            if market_id:
                resp = client.get(f"{_BASE}/markets/{market_id}")
                if resp.status_code == 200:
                    result = _parse_outcome(resp.json())
                    if result:
                        return result
                    # Not resolved yet — return early, no need for text search
                    return None

            # Fallback: text search in recent closed markets
            resp = client.get(f"{_BASE}/markets", params={
                "active": "false", "closed": "true",
                "limit": 200, "order": "endDate", "ascending": "false",
            })
            if resp.status_code != 200:
                return None
            question_lc = question.lower()[:80]
            for m in (resp.json() or []):
                mq = (m.get("question") or "").lower()[:80]
                if mq == question_lc:
                    return _parse_outcome(m)
    except Exception:
        pass
    return None


def format_for_prompt(markets: list[dict]) -> str:
    """Format Polymarket data for LLM prompt."""
    visible = [m for m in markets if m.get("question")]
    if not visible:
        return ""
    lines = ["POLYMARKET PREDICTION ODDS:"]
    for m in visible[:8]:
        prob = m.get("probability")
        prob_str = f"{prob * 100:.0f}%" if prob is not None else "?"
        vol = m.get("volume_usd", 0)
        vol_str = f"${vol / 1000:.0f}k" if vol >= 1000 else f"${vol:.0f}"
        lines.append(f"  {m['question']} → {prob_str}  (vol: {vol_str})")
    return "\n".join(lines)
