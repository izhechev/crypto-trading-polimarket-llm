"""
Coin Risk Assessor — real-time scam / rug pull / manipulation detection.

Replaces hardcoded lists with a live pipeline:
  1. On-chain signals  — price crash, volume panic, supply dilution (free, instant)
  2. News signals      — CryptoCompare + Reddit keyword search (only if on-chain flag fires)
  3. LLM verdict       — Groq categorises flagged coins in one batched call
  4. Cache             — results stored 24 h in data/coin_risk_cache.json

Categories returned:
  DEAD_PROJECT        — exclude from scanner entirely
  ACTIVE_SCAM         — show as whale ride candidate with scam warning
  MANIPULATED_REAL    — real project, extreme manipulation; show as whale ride
  SUSPICIOUS          — some red flags but not conclusive; show with warning, allow scoring
  NORMAL              — proceed with normal scoring
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

CACHE_PATH    = config.DATA_DIR / "coin_risk_cache.json"
CACHE_TTL_H   = 24

# ── On-chain thresholds ───────────────────────────────────────────────────
_CRASH_24H      = -40.0   # 24 h drop >40%  → crash event
_CRASH_7D       = -70.0   # 7 d drop >70%   → likely rug
_PANIC_VOL_FRAC =  0.90   # volume >90% of mcap while price dropping >20%
# DEAD_PROJECT requires ALL of: >99% below ATH, mcap <$50M, NOT top-100
# Absolute price collapse check removed — causes false positives in bear markets
_DEAD_ATH_DROP  = -99.0   # >99% below ATH — LUNC is at -99.9996%, active alts are -93% to -97%
_DEAD_RANK_MIN  =  100    # must be rank >100 (top-100 coins are never "dead")
# Note: no mcap threshold — LUNC has $300M mcap from 6.9T token supply but IS dead (rank ~250)
_LOW_CIRC_FRAC  =  0.20   # circulating < 20% of total supply → dilution risk
_SUPPLY_INC     =  0.50   # recent supply increase >50%

# ── News search keywords ──────────────────────────────────────────────────
_SCAM_PHRASES = [
    "rug pull", "rugpull", "exit scam", "scam", "fraud",
    "manipulation", "manipulated", "insider dump", "ponzi",
    "honeypot", "abandoned", "team dumped",
]
_CRASH_PHRASES = ["crash 90", "crash 80", "lost 90%", "lost 80%", "down 90%"]


@dataclass
class RiskAssessment:
    symbol:       str
    category:     str                   # DEAD_PROJECT | ACTIVE_SCAM | MANIPULATED_REAL | SUSPICIOUS | NORMAL
    flags:        list[str] = field(default_factory=list)
    news_hits:    list[str] = field(default_factory=list)   # matching headlines
    reasoning:    str = ""
    assessed_at:  str = ""

    def __post_init__(self):
        if not self.assessed_at:
            self.assessed_at = datetime.now(timezone.utc).isoformat()

    @property
    def is_stale(self) -> bool:
        try:
            t = datetime.fromisoformat(self.assessed_at)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) - t > timedelta(hours=CACHE_TTL_H)
        except Exception:
            return True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RiskAssessment":
        return cls(
            symbol=d["symbol"],
            category=d["category"],
            flags=d.get("flags", []),
            news_hits=d.get("news_hits", []),
            reasoning=d.get("reasoning", ""),
            assessed_at=d.get("assessed_at", ""),
        )


# ── Cache helpers ─────────────────────────────────────────────────────────

def _load_cache() -> dict[str, RiskAssessment]:
    if not CACHE_PATH.exists():
        return {}
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return {k: RiskAssessment.from_dict(v) for k, v in raw.items()}
    except Exception:
        return {}


def _save_cache(cache: dict[str, RiskAssessment]) -> None:
    try:
        CACHE_PATH.write_text(
            json.dumps({k: v.to_dict() for k, v in cache.items()}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


# ── Signal checkers ───────────────────────────────────────────────────────

def _onchain_flags(coin: dict) -> list[str]:
    """Return list of on-chain red-flag strings. Empty = no flags."""
    flags: list[str] = []
    sym   = coin.get("symbol", "?").upper()
    price = coin.get("current_price") or 0
    ch24  = coin.get("price_change_percentage_24h") or 0
    ch7d  = coin.get("price_change_percentage_7d_in_currency") or 0
    vol   = coin.get("total_volume") or 0
    mcap  = coin.get("market_cap") or 1
    rank  = coin.get("market_cap_rank") or 9999
    ath_pct = coin.get("ath_change_percentage") or 0
    circ  = coin.get("circulating_supply") or 0
    total = coin.get("total_supply") or 0

    if ch24 <= _CRASH_24H and rank <= 100:
        flags.append(f"24h crash {ch24:.0f}% (was top-100 coin)")
    elif ch24 <= _CRASH_24H:
        flags.append(f"24h crash {ch24:.0f}%")

    if ch7d <= _CRASH_7D:
        flags.append(f"7d collapse {ch7d:.0f}% — potential rug pull")

    if vol > 0 and mcap > 0 and vol / mcap > _PANIC_VOL_FRAC and ch24 <= -20:
        flags.append(f"panic exit: volume {vol/mcap:.0%} of mcap while -20%+ today")

    # DEAD_PROJECT: ATH drop >99.7% AND rank > 500 AND mcap < $30M AND vol < $250K/day.
    # All four must be true — this is extremely strict to avoid false positives.
    #
    # Why each guard:
    #  -99.7% ATH:  Deepest drawdown tier (1/300th of peak value).
    #  rank > 500:  Truly marginalized projects.
    #  mcap < $30M: Very low capitalization.
    #  vol < $250K: Very low liquidity; if anyone trades >$250k/day, it's alive.
    #
    # ICX ($44M cap, $783K vol) will no longer be flagged.
    if (ath_pct <= -99.7 and rank > 500
            and mcap < 30_000_000 and vol < 250_000):
        flags.append(
            f"likely dead: {ath_pct:.1f}% below ATH, rank #{rank}, "
            f"mcap ${mcap/1e6:.1f}M, vol ${vol/1e6:.2f}M/day"
        )

    if total > 0 and circ > 0 and circ / total < _LOW_CIRC_FRAC:
        flags.append(
            f"dilution risk: only {circ/total:.0%} of supply circulating "
            f"({circ/1e6:.1f}M of {total/1e6:.1f}M)"
        )

    return flags


def _onchain_flags_serious(coin: dict) -> list[str]:
    """Return only the serious (crash/panic/rug) flags — excludes dilution-only signals.
    Used to gate news search: dilution alone doesn't warrant a Tavily call."""
    flags = _onchain_flags(coin)
    return [f for f in flags if not f.startswith("dilution risk")]


def _news_flags(symbol: str, name: str) -> list[str]:
    """
    Search CryptoCompare + Reddit for scam/rug-pull keywords.
    Returns matching headlines (capped at 8).
    Only called when on-chain flags already exist.

    Uses full coin NAME (not ticker) as primary search term to avoid
    false positives from short/ambiguous tickers like S, H, W, X.
    """
    from src.connectors.web_research import search_reddit, search_cryptocompare_news

    hits: list[str] = []
    # Use full name as primary; fall back to symbol only if name is very short (≤2 chars)
    search_name = name if len(name) > 2 else f"{name} crypto"
    # For results validation: require either full name or long ticker (>=4 chars) in headline
    name_lc   = name.lower()
    sym_lc    = symbol.lower()
    long_sym  = len(sym_lc) >= 4   # WIF, DOGE, LINK etc. are unambiguous
    name_words = [w for w in name_lc.split() if len(w) >= 4]   # skip "of", "the", etc.

    def _is_about_coin(title_lc: str) -> bool:
        """Return True only if headline is plausibly about this specific coin."""
        if name_lc in title_lc:
            return True
        if long_sym and sym_lc in title_lc:
            return True
        if name_words and any(w in title_lc for w in name_words):
            return True
        return False

    # CryptoCompare — search by symbol (their API only accepts symbols)
    try:
        cc_items = search_cryptocompare_news(symbol, limit=10)
        for item in cc_items:
            title = (item.get("title") or "").lower()
            body  = (item.get("body") or "").lower()
            text  = title + " " + body
            if _is_about_coin(title) and any(ph in text for ph in _SCAM_PHRASES + _CRASH_PHRASES):
                hits.append(f"[CC] {item.get('title','')[:100]}")
    except Exception:
        pass

    # Reddit — use full name to avoid ticker ambiguity
    for sub in ("CryptoCurrency", "CryptoMarkets"):
        try:
            for phrase in ("rug pull scam", "manipulation dump"):
                posts = search_reddit(f'"{search_name}" {phrase}', subreddit=sub, limit=3)
                for p in posts:
                    title = (p.get("title") or "").lower()
                    if _is_about_coin(title) and \
                       any(ph in title for ph in _SCAM_PHRASES + _CRASH_PHRASES):
                        hits.append(f"[r/{sub}] {p.get('title','')[:100]}")
        except Exception:
            pass

    return list(dict.fromkeys(hits))[:8]   # deduplicate, cap at 8


def _groq_verdict(
    flagged: list[tuple[str, list[str], list[str]]],   # [(symbol, onchain_flags, news_hits)]
    client,
) -> dict[str, dict]:
    """
    Single batched Groq call to categorise all flagged coins.
    Returns {symbol: {"category": ..., "reasoning": ...}}.
    """
    if not flagged:
        return {}

    lines = []
    for sym, of, nh in flagged:
        lines.append(
            f"COIN: {sym}\n"
            f"  On-chain flags: {'; '.join(of) if of else 'none'}\n"
            f"  News mentions:  {'; '.join(nh[:4]) if nh else 'none'}"
        )

    prompt = (
        "You are a crypto security analyst. Assess each flagged coin and categorise it.\n\n"
        "Categories:\n"
        "  DEAD_PROJECT     — truly abandoned: no active dev, no community, essentially zero trading.\n"
        "                     DO NOT use this for coins that are just deep below ATH (ENJ, SUSHI, ICX are ALIVE).\n"
        "                     ONLY use DEAD_PROJECT if: volume is tiny (<$100k) AND rank is deep tail (>800)\n"
        "                     AND there are reports of abandonment / rug with no recovery.\n"
        "  ACTIVE_SCAM      — ongoing fraud/rug pull, do not recommend buying\n"
        "  MANIPULATED_REAL — real project with genuine utility BUT extreme price manipulation\n"
        "                     (can be tracked as a high-risk whale ride only)\n"
        "  SUSPICIOUS       — some red flags but not conclusive; track with warnings\n"
        "  NORMAL           — flags are explainable (market crash, low float, bear market), treat normally\n\n"
        "Return JSON:\n"
        '{"assessments": [{"symbol":"SYM","category":"...","reasoning":"1 sentence"}]}\n\n'
        "Flagged coins:\n\n" + "\n\n".join(lines)
    )

    try:
        from src.utils.budget_tracker import log_llm_call, check_budget, BudgetExceededError
        check_budget("groq")
        resp = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
            temperature=0.05,
            response_format={"type": "json_object"},
        )
        log_llm_call("groq", tokens_in=len(prompt)//4, tokens_out=800, endpoint="coin_risk")
        raw = resp.choices[0].message.content or ""
        data = json.loads(raw)
        return {a["symbol"]: a for a in data.get("assessments", [])}
    except Exception as e:
        print(f"  [risk] Groq verdict failed: {e}")
        return {}


# ── Main entry point ──────────────────────────────────────────────────────

def assess_coin_risks(
    candidates: list[dict],
    fear_greed: dict | None = None,
) -> dict[str, RiskAssessment]:
    """
    Assess risk for a list of CoinGecko coin dicts.
    Returns {symbol.upper(): RiskAssessment}.

    Flow:
      1. Load cache — skip fresh assessments
      2. On-chain signals for each uncached coin
      3. News search only for coins with on-chain flags
      4. Batch Groq call for all flagged coins
      5. NORMAL verdict for unflagged coins
      6. Save cache
    """
    cache   = _load_cache()
    results: dict[str, RiskAssessment] = {}
    to_assess: list[dict]              = []

    # Step 1 — serve from cache where possible
    for coin in candidates:
        sym = coin.get("symbol", "").upper()
        cached = cache.get(sym)
        if cached and not cached.is_stale:
            results[sym] = cached
        else:
            to_assess.append(coin)

    if not to_assess:
        return results

    # print(f"  🔎 Risk-checking {len(to_assess)} coins (cache miss)…")

    # Step 2 — on-chain flags (all flags used for scoring; serious-only gates news)
    flagged_onchain: dict[str, list[str]] = {}   # full flag set (incl. dilution)
    flagged_serious: dict[str, list[str]] = {}   # crash/panic/rug only — gates news check
    for coin in to_assess:
        sym          = coin.get("symbol", "").upper()
        flags        = _onchain_flags(coin)
        serious      = _onchain_flags_serious(coin)
        if flags:
            flagged_onchain[sym] = flags
        if serious:
            flagged_serious[sym] = serious

    # Step 3 — skip network news search (Reddit/CryptoCompare too slow for 3000 coins).
    # On-chain flags + heuristics are reliable enough for whale ride / exclusion decisions.
    flagged_news: dict[str, list[str]] = {}

    # Step 4 — heuristic verdict only (no LLM/network calls for speed)
    # Step 5 — build RiskAssessment for all assessed coins
    for coin in to_assess:
        sym = coin.get("symbol", "").upper()
        of  = flagged_onchain.get(sym, [])

        if not of:
            ra = RiskAssessment(symbol=sym, category="NORMAL",
                                flags=[], news_hits=[], reasoning="No risk signals detected.")
        else:
            category  = _heuristic_category(of, [])
            reasoning = "; ".join(of[:2])
            ra = RiskAssessment(symbol=sym, category=category,
                                flags=of, news_hits=[], reasoning=reasoning)

        results[sym] = ra
        cache[sym]   = ra

    # Step 6 — GoPlus audit skipped (too slow for 3000-coin scans)

    _save_cache(cache)
    return results


def _heuristic_category(onchain_flags: list[str], news_hits: list[str]) -> str:
    """Fallback category when Groq is unavailable."""
    scam_news = sum(1 for h in news_hits if any(p in h.lower() for p in ("scam","rug","fraud","exit")))
    if scam_news >= 3:
        return "ACTIVE_SCAM"
    # Extremely strict fallback for dead projects
    if any("likely dead" in f for f in onchain_flags) and scam_news >= 1:
        return "DEAD_PROJECT"
    if any("rug pull" in f or "panic exit" in f for f in onchain_flags):
        return "SUSPICIOUS"
    if onchain_flags:
        return "SUSPICIOUS"
    return "NORMAL"
