"""Kraken exchange connector — fetch live portfolio balances via ccxt."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

# ccxt normalises most Kraken tickers (XBT→BTC) but a few slip through
_REMAP = {"XBT": "BTC", "XDG": "DOGE"}

# Symbol → CoinGecko coin ID
_COIN_IDS: dict[str, str] = {
    "BTC":    "bitcoin",
    "ETH":    "ethereum",
    "SOL":    "solana",
    "XRP":    "ripple",
    "ADA":    "cardano",
    "DOT":    "polkadot",
    "LINK":   "chainlink",
    "AVAX":   "avalanche-2",
    "ATOM":   "cosmos",
    "LTC":    "litecoin",
    "BCH":    "bitcoin-cash",
    "UNI":    "uniswap",
    "AAVE":   "aave",
    "MKR":    "maker",
    "COMP":   "compound-governance-token",
    "GRT":    "the-graph",
    "CRV":    "curve-dao-token",
    "SNX":    "havven",
    "BAT":    "basic-attention-token",
    "FIL":    "filecoin",
    "INJ":    "injective-protocol",
    "RENDER": "render-token",
    "NEAR":   "near",
    "OP":     "optimism",
    "ARB":    "arbitrum",
    "SUI":    "sui",
    "APT":    "aptos",
    "TIA":    "celestia",
    "SEI":    "sei-network",
    "PEPE":   "pepe",
    "DOGE":   "dogecoin",
    "SHIB":   "shiba-inu",
    "MATIC":  "matic-network",
    "FTM":    "fantom",
    "ALGO":   "algorand",
    "XLM":    "stellar",
    "TRX":    "tron",
    "ETC":    "ethereum-classic",
    "XMR":    "monero",
    "ZEC":    "zcash",
    "DASH":   "dash",
    "MANA":   "decentraland",
    "SAND":   "the-sandbox",
    "AXS":    "axie-infinity",
    "ENJ":    "enjincoin",
    "LRC":    "loopring",
    "STORJ":  "storj",
    "OCEAN":  "ocean-protocol",
    "KAVA":   "kava",
    "1INCH":  "1inch",
    "SUSHI":  "sushi",
    "ZRX":    "0x",
    "ENS":    "ethereum-name-service",
    "IMX":    "immutable-x",
    "BLUR":   "blur",
    "STX":    "blockstack",
    "RUNE":   "thorchain",
    "KSM":    "kusama",
    "FLOW":   "flow",
    "CHZ":    "chiliz",
    "HBAR":   "hedera-hashgraph",
    "ICP":    "internet-computer",
    "EGLD":   "elrond-erd-2",
    "THETA":  "theta-token",
    "VET":    "vechain",
    "WIF":    "dogwifcoin",
    "BONK":   "bonk",
    "ENA":    "ethena",
}

_QUOTE_CURRENCIES = {"EUR", "USD", "GBP", "USDT", "USDC", "BTC", "ETH"}

_FIAT_AND_STABLES = {
    "EUR", "USD", "GBP", "CHF", "CAD", "AUD", "JPY",
    "ZEUR", "ZUSD", "ZGBP", "ZCAD", "ZAUD", "ZJPY",
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "USDD", "FDUSD",
    "PYUSD", "GUSD", "FRAX", "LUSD", "SUSD", "USDE", "USDS",
}


def _collect_raw(exchange) -> dict[str, float]:
    """
    Try every known method to get raw Kraken balances.
    Returns the raw symbol dict (e.g. {"INJ": 0.007, "INJ.S": 33.0, ...}).
    Keys are NOT yet normalised — that happens in _normalize_and_merge.
    """
    raw: dict[str, float] = {}

    def _absorb(bal: dict) -> None:
        for sym, amt in bal.get("total", {}).items():
            if amt and float(amt) > 0:
                raw[sym] = raw.get(sym, 0.0) + float(amt)

    # Method 1: type='all'
    try:
        _absorb(exchange.fetch_balance({"type": "all"}))
        if raw:
            return raw
    except Exception:
        pass

    # Method 2: individual balance types (spot + staking + earn)
    for btype in ("spot", "staking", "earn"):
        try:
            _absorb(exchange.fetch_balance({"type": btype}))
        except Exception:
            pass

    if raw:
        return raw

    # Method 3: plain default
    try:
        _absorb(exchange.fetch_balance())
    except Exception:
        pass

    return raw


def _normalize_and_merge(raw: dict[str, float]) -> dict[str, float]:
    """
    Normalize raw Kraken symbols and sum staking/earn variants into one entry.

    Kraken appends suffixes to staked/earning assets:
      INJ.S  → staked INJ
      ETH.F  → flexible-earn ETH
      DOT.B  → bonded DOT
      XBT.M  → margin BTC

    All variants are merged so the caller sees one total per base asset.
    """
    merged: dict[str, float] = {}
    _STAKING_SUFFIXES = (".S", ".F", ".B", ".M", ".W", ".X")

    for raw_sym, amount in raw.items():
        sym = raw_sym.upper()
        for sfx in _STAKING_SUFFIXES:
            if sym.endswith(sfx):
                sym = sym[: -len(sfx)]
                break
        sym = _REMAP.get(sym, sym)
        merged[sym] = merged.get(sym, 0.0) + amount

    return merged


def fetch_kraken_portfolio() -> tuple[list[dict] | None, str]:
    """
    Fetch live balances from Kraken (spot + staking + earn, merged per asset).
    Kraken amounts are the source of truth — portfolio.json is consulted only
    for entry prices, never to override amounts.

    Returns (holdings, source_description):
      holdings = None   → Kraken unreachable; caller falls back to portfolio.json
      holdings = [...]  → real Kraken balances (may include dust — caller filters)
    """
    if not config.KRAKEN_API_KEY or not config.KRAKEN_PRIVATE_KEY:
        return None, "portfolio.json (no Kraken credentials)"

    try:
        import ccxt
        exchange = ccxt.kraken({
            "apiKey":  config.KRAKEN_API_KEY,
            "secret":  config.KRAKEN_PRIVATE_KEY,
        })
        raw = _collect_raw(exchange)
    except Exception as e:
        return None, f"portfolio.json (Kraken API error: {e})"

    if not raw:
        return None, "portfolio.json (Kraken returned no balances)"

    normalized = _normalize_and_merge(raw)

    # Entry prices only — amounts come from Kraken, not portfolio.json
    pf_map: dict[str, dict] = {}
    try:
        with open(config.PORTFOLIO_PATH) as f:
            pf = json.load(f)
        for h in pf.get("holdings", []):
            pf_map[h["asset"].upper()] = h
    except Exception:
        pass

    holdings = []
    for symbol, amount in normalized.items():
        if symbol in _FIAT_AND_STABLES:
            continue

        coin_id  = _COIN_IDS.get(symbol)
        pf_entry = pf_map.get(symbol, {})

        holdings.append({
            "asset":           symbol,
            "coin_id":         coin_id,
            "amount":          round(amount, 8),
            "entry_price_usd": pf_entry.get("entry_price_usd"),
        })

    return holdings, "Kraken API"


def fetch_trade_history() -> dict[str, dict]:
    """
    Fetch all buy trades from Kraken and compute, per asset:
      - weighted average entry price in USD
      - total amount bought
      - first buy date
      - total fees paid in USD (approximate)

    Returns {SYMBOL: {"avg_entry_usd": float, "total_amount": float,
                       "first_buy": str, "total_fees_usd": float}}
    Returns {} if credentials are missing or the API call fails.
    """
    if not config.KRAKEN_API_KEY or not config.KRAKEN_PRIVATE_KEY:
        return {}

    try:
        import ccxt
        exchange = ccxt.kraken({
            "apiKey":  config.KRAKEN_API_KEY,
            "secret":  config.KRAKEN_PRIVATE_KEY,
        })
        trades = exchange.fetch_my_trades()
    except Exception as e:
        print(f"  Warning: Kraken trade history failed ({e})")
        return {}

    # Accumulate weighted average for each base asset
    # trades are dicts with keys: symbol, side, price, amount, cost, fee, datetime
    totals: dict[str, dict] = {}

    for t in trades:
        if t.get("side") != "buy":
            continue

        # ccxt symbol format: "INJ/EUR" or "INJ/USD"
        raw_symbol = t.get("symbol", "")
        parts = raw_symbol.split("/")
        if len(parts) != 2:
            continue
        base, quote = parts[0].upper(), parts[1].upper()
        base = _REMAP.get(base, base)

        if base in _FIAT_AND_STABLES:
            continue

        price_quote = t.get("price", 0) or 0
        amount      = t.get("amount", 0) or 0
        fee_cost    = (t.get("fee") or {}).get("cost") or 0
        fee_currency= (t.get("fee") or {}).get("currency", quote)
        dt_str      = t.get("datetime", "")

        # Convert price to USD approximation — for EUR quotes multiply by ~1.09
        # (rough, but avoids an extra API call; P&L% stays currency-neutral)
        if quote == "EUR":
            price_usd = price_quote * 1.09
            fee_usd   = float(fee_cost) * 1.09 if fee_currency == "EUR" else float(fee_cost)
        else:
            price_usd = price_quote
            fee_usd   = float(fee_cost)

        if base not in totals:
            totals[base] = {
                "cost_usd":    0.0,
                "total_amount": 0.0,
                "total_fees_usd": 0.0,
                "first_buy":   dt_str,
            }

        totals[base]["cost_usd"]      += price_usd * float(amount)
        totals[base]["total_amount"]  += float(amount)
        totals[base]["total_fees_usd"] += fee_usd
        # Keep earliest trade date
        if dt_str and dt_str < totals[base]["first_buy"]:
            totals[base]["first_buy"] = dt_str

    result = {}
    for asset, d in totals.items():
        if d["total_amount"] > 0:
            result[asset] = {
                "avg_entry_usd":  round(d["cost_usd"] / d["total_amount"], 6),
                "total_amount":   round(d["total_amount"], 8),
                "first_buy":      d["first_buy"][:10] if d["first_buy"] else "",
                "total_fees_usd": round(d["total_fees_usd"], 4),
            }
    return result
