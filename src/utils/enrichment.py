"""
Enrichment orchestrator — fetches data from all enrichment APIs and
formats a single block of context for the Groq LLM prompt.

Each source is wrapped in a try/except so one failing API never blocks the rest.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.connectors.coinmarketcap import fetch_trending, fetch_gainers_losers, format_for_prompt as _fmt_cmc
from src.connectors.messari       import fetch_metrics_batch,                  format_for_prompt as _fmt_messari
from src.connectors.etherscan     import fetch_eth_price, fetch_gas_price,     format_for_prompt as _fmt_etherscan
from src.connectors.defillama     import (
    fetch_total_tvl, fetch_top_protocols, fetch_tvl_for_coins,
    format_for_prompt as _fmt_defillama,
)
from src.connectors.coinpaprika   import fetch_events_for_coins,               format_for_prompt as _fmt_coinpaprika
from src.connectors.polymarket    import fetch_crypto_markets,                 format_for_prompt as _fmt_polymarket


def fetch_enrichment(top_symbols: list[str]) -> str:
    """
    Fetch enrichment data for the top scanner coins.

    top_symbols — list of coin symbols (e.g. ["SOL", "NEAR", "INJ"])

    Returns a formatted multi-section string ready to inject into the Groq prompt.
    Returns "" if all sources fail.
    """
    sections: list[str] = []

    # ── CoinMarketCap — trending + gainers/losers ─────────────────────────
    try:
        trending = fetch_trending()
        gl       = fetch_gainers_losers()
        text     = _fmt_cmc(trending, gl)
        if text:
            sections.append(text)
            print(f"  [enrichment] CMC: {len(trending)} trending, {len(gl.get('gainers', []))} gainers")
    except Exception as e:
        print(f"  [enrichment] CMC failed: {e}")

    # ── Messari — developer activity + ATH distance ───────────────────────
    try:
        metrics = fetch_metrics_batch(top_symbols[:5])
        text    = _fmt_messari(metrics)
        if text:
            sections.append(text)
            print(f"  [enrichment] Messari: {len(metrics)} coins")
    except Exception as e:
        print(f"  [enrichment] Messari failed: {e}")

    # ── Etherscan — ETH price + gas oracle ───────────────────────────────
    try:
        eth_price = fetch_eth_price()
        gas       = fetch_gas_price()
        text      = _fmt_etherscan(eth_price, gas)
        if text:
            sections.append(text)
            print(f"  [enrichment] Etherscan: ETH=${eth_price.get('eth_usd', '?')}, gas={gas.get('fast_gwei', '?')} Gwei")
    except Exception as e:
        print(f"  [enrichment] Etherscan failed: {e}")

    # ── DeFiLlama — total TVL + protocol TVLs ────────────────────────────
    try:
        total_tvl     = fetch_total_tvl()
        top_protocols = fetch_top_protocols(5)
        coin_tvls     = fetch_tvl_for_coins(top_symbols)
        text          = _fmt_defillama(total_tvl, top_protocols, coin_tvls)
        if text:
            sections.append(text)
            tvl_b = f"${total_tvl/1e9:.1f}B" if total_tvl else "?"
            print(f"  [enrichment] DeFiLlama: total TVL {tvl_b}, {len(coin_tvls)} coin TVLs")
    except Exception as e:
        print(f"  [enrichment] DeFiLlama failed: {e}")

    # ── CoinPaprika — upcoming events ─────────────────────────────────────
    try:
        events = fetch_events_for_coins(top_symbols)
        text   = _fmt_coinpaprika(events)
        if text:
            sections.append(text)
            print(f"  [enrichment] CoinPaprika: events for {list(events.keys())}")
    except Exception as e:
        print(f"  [enrichment] CoinPaprika failed: {e}")

    # ── Polymarket — prediction market odds ──────────────────────────────
    try:
        markets = fetch_crypto_markets()
        text    = _fmt_polymarket(markets)
        if text:
            sections.append(text)
            print(f"  [enrichment] Polymarket: {len(markets)} markets")
    except Exception as e:
        print(f"  [enrichment] Polymarket failed: {e}")

    return "\n\n".join(sections)
