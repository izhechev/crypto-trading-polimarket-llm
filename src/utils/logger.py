"""Recommendation logger with live position tracking."""
import csv
import json
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

LOG_PATH          = config.DATA_DIR / "recommendations.csv"
HISTORY_PATH      = config.DATA_DIR / "price_history.csv"
DUST_THRESHOLD_EUR = 0.10   # holdings below this are labelled "dust"

_HEADERS = [
    "date", "type", "coin", "coin_id",
    "entry_price",    # USD
    "stop_loss",      # USD
    "take_profit",    # USD
    "status",         # OPEN / WIN / LOSS / "" (watchlist)
    "exit_price",     # USD, filled when closed
    "pnl_pct",        # % — currency-neutral
    "current_price",  # USD
    "price_eur",      # EUR
    "timeframe", "fear_greed", "reasoning",
]

_HISTORY_HEADERS = ["timestamp", "coin", "coin_id", "price_eur", "price_usd"]

_W = 48  # section divider width


# ── Internal helpers ──────────────────────────────────────────────────────

def _read() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    with open(LOG_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write(rows: list[dict]) -> None:
    # SAFETY: never silently drop OPEN scanner positions.
    open_scanner = [r for r in rows if r.get("type", "") == "SCANNER" and r.get("status") == "OPEN"]
    existing_open = [r for r in _read() if r.get("type", "") == "SCANNER" and r.get("status") == "OPEN"]
    existing_coins = {r["coin"].upper() for r in existing_open}
    new_coins      = {r["coin"].upper() for r in open_scanner}
    dropped = existing_coins - new_coins
    if dropped:
        raise RuntimeError(
            f"BUG: _write() would delete OPEN scanner positions: {dropped}. "
            "Aborting write to protect trade history."
        )
    with open(LOG_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_HEADERS, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)


def _latest_per_coin(rows: list[dict]) -> dict[str, dict]:
    """Return the most-recent row keyed by coin symbol."""
    latest: dict[str, dict] = {}
    for r in rows:
        coin = r["coin"]
        if coin not in latest or r["date"] > latest[coin]["date"]:
            latest[coin] = r
    return latest


def _fmt(eur: float, usd: float) -> str:
    """Format as '€X.XXXX ($Y.YYYY)' choosing decimal places by magnitude."""
    decimals = 2 if eur >= 1 else 4
    return f"€{eur:.{decimals}f} (${usd:.{decimals}f})"


# ── Public API ────────────────────────────────────────────────────────────

def log_recommendation(rec: dict, fear_greed_value: int) -> None:
    """
    Append a new scanner recommendation with status=OPEN and type=SCANNER.
    Skips if the same coin already has an OPEN scanner position.
    """
    rows = _read()
    coin = rec.get("coin", "").upper()

    # Duplicate check — avoid logging the same pick while it's still open.
    # Old rows written before the type column was added have type="" which also
    # counts as a scanner entry (the default fallback was "SCANNER").
    already_open = any(
        r.get("type", "") in ("SCANNER", "")
        and r.get("status") == "OPEN"
        and r.get("coin", "").upper() == coin
        for r in rows
    )
    if already_open:
        print(f"  Skipped — {coin} already has an OPEN scanner position")
        return

    rows.append({
        "date":          datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "type":          "SCANNER",
        "coin":          coin,
        "coin_id":       rec.get("coin_id", ""),
        "entry_price":   rec.get("entry_price", ""),
        "stop_loss":     rec.get("stop_loss", ""),
        "take_profit":   rec.get("take_profit", ""),
        "status":        "OPEN",
        "exit_price":    "",
        "pnl_pct":       "",
        "current_price": rec.get("entry_price", ""),
        "price_eur":     "",
        "timeframe":     rec.get("timeframe", ""),
        "fear_greed":    fear_greed_value,
        "reasoning":     rec.get("reasoning", "").replace("\n", " "),
    })
    _write(rows)
    print(f"  Logged → {LOG_PATH}")


def log_scanner_results(top10: list[dict], fear_greed_value: int) -> None:
    """
    Log every coin in the top-10 scanner results as an OPEN scanner pick.
    Auto-calculates SL = entry × 0.80 and TP = entry × 1.50 for coins that
    don't already have an OPEN position.  Groq can later sharpen SL/TP via
    update_scanner_sltp().
    """
    logged = 0
    for r in top10:
        entry = r.get("price", 0)
        if not entry:
            continue
        reasons = r.get("reasons", [])
        rec = {
            "coin":        r["symbol"],
            "coin_id":     r["coin_id"],
            "entry_price": round(entry, 8),
            "stop_loss":   round(entry * 0.80, 8),
            "take_profit": round(entry * 1.30, 8),
            "timeframe":   "3-7 days",
            "reasoning":   f"Score {r['score']}. " + ", ".join(reasons),
        }
        rows_before = len(_read())
        log_recommendation(rec, fear_greed_value)
        if len(_read()) > rows_before:
            logged += 1
    if logged:
        print(f"  {logged} new scanner picks logged")


def update_scanner_sltp(
    coin: str,
    stop_loss: float,
    take_profit: float,
    reasoning: str = "",
) -> None:
    """
    Update SL/TP (and optionally reasoning) for an existing OPEN scanner position.
    Called after Groq analysis to sharpen the auto-calculated levels.
    """
    rows = _read()
    for row in rows:
        if (row.get("type", "") in ("SCANNER", "")
                and row.get("status") == "OPEN"
                and row.get("coin", "").upper() == coin.upper()):
            row["stop_loss"]   = stop_loss
            row["take_profit"] = take_profit
            if reasoning:
                row["reasoning"] = reasoning.replace("\n", " ")
            break
    _write(rows)
    print(f"  SL/TP updated for {coin.upper()} → SL ${stop_loss:.6f}, TP ${take_profit:.6f}")


def update_open_positions() -> None:
    """
    Refresh prices for all OPEN SCANNER positions.
    - current >= take_profit → WIN
    - current <= stop_loss   → LOSS
    - otherwise              → stays OPEN, updates current_price + price_eur + pnl_pct
    All price comparisons use USD (matches entry_price currency).
    """
    rows = _read()
    scanner_open = [
        r for r in rows
        if r.get("status") == "OPEN"
        and r.get("coin_id")
        and r.get("type", "SCANNER") == "SCANNER"
    ]
    if not scanner_open:
        return

    from src.connectors.coingecko import fetch_prices
    coin_ids = list({r["coin_id"] for r in scanner_open})
    try:
        price_objs = fetch_prices(coin_ids)
        usd_map = {p.coin_id: p.price_usd for p in price_objs}
        eur_map = {p.coin_id: p.price_eur for p in price_objs}
    except Exception as e:
        print(f"  Warning: could not fetch prices for tracking: {e}")
        return

    closed = 0
    for row in rows:
        if (row.get("status") != "OPEN"
                or not row.get("coin_id")
                or row.get("type", "SCANNER") != "SCANNER"):
            continue
        usd = usd_map.get(row["coin_id"])
        if usd is None:
            continue

        try:
            entry = float(row["entry_price"])
            sl    = float(row["stop_loss"])
            tp    = float(row["take_profit"])
        except (ValueError, KeyError):
            continue

        pnl_pct = (usd - entry) / entry * 100
        row["current_price"] = round(usd, 6)
        row["price_eur"]     = round(eur_map.get(row["coin_id"], 0), 6)
        row["pnl_pct"]       = round(pnl_pct, 2)

        if usd >= tp:
            row["status"]     = "WIN"
            row["exit_price"] = round(usd, 6)
            closed += 1
        elif usd <= sl:
            row["status"]     = "LOSS"
            row["exit_price"] = round(usd, 6)
            closed += 1

    _write(rows)
    if closed:
        print(f"  {closed} position(s) closed (WIN/LOSS)")


def log_portfolio_positions() -> None:
    """
    Fetch holdings (Kraken live → portfolio.json fallback) and log
    a snapshot row per holding with type=PORTFOLIO.
    reasoning: "amount:16|src:Kraken"
    """
    from src.connectors.kraken import fetch_kraken_portfolio
    from src.connectors.coingecko import fetch_prices

    holdings, source = fetch_kraken_portfolio()
    if holdings is None:
        try:
            with open(config.PORTFOLIO_PATH) as f:
                pf = json.load(f)
            holdings = pf.get("holdings", [])
            source = "portfolio.json"
        except Exception as e:
            print(f"  Warning: could not load portfolio: {e}")
            return

    if not holdings:
        return

    coin_ids = [h["coin_id"] for h in holdings if h.get("coin_id")]
    if not coin_ids:
        print("  Warning: no coin IDs resolved for portfolio holdings")
        return

    try:
        price_objs = fetch_prices(coin_ids)
        usd_map = {p.coin_id: p.price_usd for p in price_objs}
        eur_map = {p.coin_id: p.price_eur for p in price_objs}
    except Exception as e:
        print(f"  Warning: could not fetch portfolio prices: {e}")
        return

    rows = _read()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    logged = 0

    for h in holdings:
        coin_id = h.get("coin_id")
        if not coin_id:
            continue
        usd = usd_map.get(coin_id)
        eur = eur_map.get(coin_id)
        if usd is None:
            continue

        entry = h.get("entry_price_usd")
        pnl_pct = round((usd - entry) / entry * 100, 2) if entry else ""

        rows.append({
            "date":          now,
            "type":          "PORTFOLIO",
            "coin":          h["asset"],
            "coin_id":       coin_id,
            "entry_price":   entry if entry is not None else "",
            "stop_loss":     "",
            "take_profit":   "",
            "status":        "OPEN",
            "exit_price":    "",
            "pnl_pct":       pnl_pct,
            "current_price": round(usd, 6),
            "price_eur":     round(eur, 6) if eur else "",
            "timeframe":     "",
            "fear_greed":    "",
            "reasoning":     f"amount:{h['amount']}|src:{source}",
        })
        logged += 1

    _write(rows)
    print(f"  Portfolio positions logged ({logged} holdings, source: {source})")


def log_watchlist_prices() -> None:
    """
    Fetch current prices for WATCHLIST_TRACK coins and log with type=WATCHLIST.
    reasoning: "7d:-4.52"
    """
    if not getattr(config, "WATCHLIST_TRACK", None):
        return

    from src.connectors.coingecko import fetch_prices
    try:
        price_list = fetch_prices(config.WATCHLIST_TRACK)
    except Exception as e:
        print(f"  Warning: could not fetch watchlist prices: {e}")
        return

    rows = _read()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    for p in price_list:
        rows.append({
            "date":          now,
            "type":          "WATCHLIST",
            "coin":          p.symbol,
            "coin_id":       p.coin_id,
            "entry_price":   "",
            "stop_loss":     "",
            "take_profit":   "",
            "status":        "",
            "exit_price":    "",
            "pnl_pct":       "",
            "current_price": round(p.price_usd, 6),
            "price_eur":     round(p.price_eur, 6),
            "timeframe":     "",
            "fear_greed":    "",
            "reasoning":     f"7d:{p.change_7d:.2f}",
        })

    _write(rows)
    print(f"  Watchlist prices logged ({len(price_list)} coins)")


def log_price_history() -> None:
    """
    Append EUR+USD prices for all tracked coins to price_history.csv.
    Tracked = portfolio holdings + watchlist + open scanner positions.
    """
    from src.connectors.coingecko import fetch_prices

    coin_ids: set[str] = set()

    # Portfolio holdings
    try:
        with open(config.PORTFOLIO_PATH) as f:
            pf = json.load(f)
        for h in pf.get("holdings", []):
            if h.get("coin_id"):
                coin_ids.add(h["coin_id"])
    except Exception:
        pass

    # Watchlist
    for cid in getattr(config, "WATCHLIST_TRACK", []):
        coin_ids.add(cid)

    # Open scanner positions
    for r in _read():
        if (r.get("type", "SCANNER") == "SCANNER"
                and r.get("status") == "OPEN"
                and r.get("coin_id")):
            coin_ids.add(r["coin_id"])

    if not coin_ids:
        return

    try:
        prices = fetch_prices(list(coin_ids))
    except Exception as e:
        print(f"  Warning: price history fetch failed: {e}")
        return

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    write_header = not HISTORY_PATH.exists()
    try:
        fh = open(HISTORY_PATH, "a", newline="", encoding="utf-8")
    except PermissionError:
        print(f"  Warning: {HISTORY_PATH.name} is locked (open in another program?) — price history skipped")
        return
    with fh as f:
        writer = csv.DictWriter(f, fieldnames=_HISTORY_HEADERS)
        if write_header:
            writer.writeheader()
        for p in prices:
            writer.writerow({
                "timestamp": now,
                "coin":      p.symbol,
                "coin_id":   p.coin_id,
                "price_eur": round(p.price_eur, 6),
                "price_usd": round(p.price_usd, 6),
            })

    print(f"  Price history logged ({len(prices)} coins → {HISTORY_PATH.name})")


def print_track_record() -> None:
    """Print a P&L summary: PORTFOLIO · WATCHLIST · SCANNER PICKS."""
    rows = _read()
    if not rows:
        print("\n  No data logged yet.")
        return

    # ── 1. PORTFOLIO ──────────────────────────────────────────────────────
    portfolio_rows = [r for r in rows if r.get("type") == "PORTFOLIO"]
    print(f"\n  {'─'*_W}")
    print(f"  PORTFOLIO  (Kraken live / portfolio.json fallback)")
    print(f"  {'─'*_W}")

    if portfolio_rows:
        latest = _latest_per_coin(portfolio_rows)

        # Parse amounts from reasoning "amount:16|src:Kraken"
        amounts: dict[str, float] = {}
        for coin, r in latest.items():
            part = r.get("reasoning", "").split("|")[0]
            if part.startswith("amount:"):
                try:
                    amounts[coin] = float(part[7:])
                except ValueError:
                    pass
        if not amounts:
            try:
                with open(config.PORTFOLIO_PATH) as f:
                    pf = json.load(f)
                for h in pf.get("holdings", []):
                    amounts[h["asset"]] = h["amount"]
            except Exception:
                pass

        # Try to get trade history from Kraken for precise entry prices + fees
        trade_history: dict[str, dict] = {}
        try:
            from src.connectors.kraken import fetch_trade_history
            trade_history = fetch_trade_history()
        except Exception:
            pass

        # Build high/low per coin since first buy date from price_history.csv
        price_highs: dict[str, float] = {}
        price_lows:  dict[str, float] = {}
        if HISTORY_PATH.exists():
            try:
                with open(HISTORY_PATH, newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        c = row.get("coin", "")
                        try:
                            eur_p = float(row.get("price_eur") or 0)
                            if eur_p <= 0:
                                continue
                            price_highs[c] = max(price_highs.get(c, 0), eur_p)
                            price_lows[c]  = min(price_lows.get(c, eur_p), eur_p)
                        except ValueError:
                            pass
            except Exception:
                pass

        total_value_eur    = 0.0
        total_cost_eur_est = 0.0

        for coin, r in sorted(latest.items()):
            try:
                usd = float(r["current_price"])
                eur_raw = r.get("price_eur", "")
                eur = float(eur_raw) if eur_raw else usd * 0.92
                amt = amounts.get(coin, 0.0)
                eur_value = amt * eur

                if eur_value < DUST_THRESHOLD_EUR:
                    print(f"    [dust] {coin:8s}  {_fmt(eur, usd)}  value €{eur_value:.2f}")
                    continue

                rate = eur / usd if usd else 0.92  # current EUR/USD ratio

                # Use Kraken trade history entry if available, else fall back to CSV entry_price
                trade = trade_history.get(coin)
                if trade:
                    entry_usd = trade["avg_entry_usd"]
                    first_buy = trade["first_buy"]
                    fees_usd  = trade["total_fees_usd"]
                    fees_eur  = fees_usd * rate
                    entry_eur = entry_usd * rate
                    source_tag = "Kraken trades"
                else:
                    entry_raw = r.get("entry_price", "")
                    entry_usd = float(entry_raw) if entry_raw else None
                    entry_eur = entry_usd * rate if entry_usd else None
                    first_buy = ""
                    fees_eur  = None
                    source_tag = "portfolio.json"

                if entry_eur:
                    cost_eur       = amt * entry_eur
                    pnl_eur        = eur_value - cost_eur - (fees_eur or 0)
                    pnl_pct        = pnl_eur / cost_eur * 100 if cost_eur else 0
                    total_cost_eur_est += cost_eur
                    total_value_eur    += eur_value
                    icon = "+" if pnl_eur >= 0 else "-"
                    entry_str = f"entry €{entry_eur:.4f}" + (f" on {first_buy}" if first_buy else "")
                    fee_str   = f"  fee €{fees_eur:.2f}" if fees_eur else ""
                    pnl_str   = f"P&L: €{pnl_eur:+.2f} ({pnl_pct:+.1f}%)"
                    high_str  = f"  High: €{price_highs[coin]:.4f}" if coin in price_highs else ""
                    low_str   = f"  Low: €{price_lows[coin]:.4f}"   if coin in price_lows  else ""
                    print(
                        f"    [{icon}] {coin:8s}  {amt:.4f} × {entry_str}{fee_str}\n"
                        f"           now {_fmt(eur, usd)}  value €{eur_value:.2f}"
                        f"  {pnl_str}{high_str}{low_str}"
                    )
                else:
                    total_value_eur += eur_value
                    print(
                        f"    [ ] {coin:8s}  now {_fmt(eur, usd)}"
                        f"  value €{eur_value:.2f}  (no entry price)"
                    )
            except (ValueError, KeyError):
                pass

        total_pnl_eur = total_value_eur - total_cost_eur_est
        total_pnl_pct = (total_pnl_eur / total_cost_eur_est * 100) if total_cost_eur_est else 0
        icon = "+" if total_pnl_eur >= 0 else "-"
        print(
            f"\n    [{icon}] TOTAL  invested ≈€{total_cost_eur_est:.2f}"
            f"  now €{total_value_eur:.2f}  ({total_pnl_pct:+.1f}%)"
        )
    else:
        print("    No portfolio data yet — run with --scan to populate.")

    # ── 2. WATCHLIST ──────────────────────────────────────────────────────
    # Coins already shown in PORTFOLIO are excluded — portfolio takes priority.
    portfolio_coins = set(_latest_per_coin(portfolio_rows).keys()) if portfolio_rows else set()

    watchlist_rows = [r for r in rows if r.get("type") == "WATCHLIST"]
    print(f"\n  {'─'*_W}")
    print(f"  WATCHLIST  (monitored, not owned)")
    print(f"  {'─'*_W}")

    if watchlist_rows:
        latest_wl = {
            coin: r
            for coin, r in _latest_per_coin(watchlist_rows).items()
            if coin not in portfolio_coins
        }
        for coin, r in sorted(latest_wl.items()):
            try:
                usd = float(r["current_price"])
                eur_raw = r.get("price_eur", "")
                eur = float(eur_raw) if eur_raw else usd * 0.92
                reasoning = r.get("reasoning", "")
                change_7d: float | None = None
                if reasoning.startswith("7d:"):
                    try:
                        change_7d = float(reasoning[3:])
                    except ValueError:
                        pass
                suffix = f"  (7d: {change_7d:+.1f}%)" if change_7d is not None else ""
                print(f"    {coin:8s}  {_fmt(eur, usd)}{suffix}")
            except (ValueError, KeyError):
                pass
    else:
        print("    No watchlist data yet — run with --scan to populate.")

    # ── 3. SCANNER PICKS ─────────────────────────────────────────────────
    scanner_rows = [
        r for r in rows
        if r.get("type", "SCANNER") not in ("PORTFOLIO", "WATCHLIST")
    ]
    total    = len(scanner_rows)
    n_open   = sum(1 for r in scanner_rows if r.get("status") == "OPEN")
    n_win    = sum(1 for r in scanner_rows if r.get("status") == "WIN")
    n_loss   = sum(1 for r in scanner_rows if r.get("status") == "LOSS")
    closed   = n_win + n_loss
    win_rate = (n_win / closed * 100) if closed else 0

    pnls = []
    for r in scanner_rows:
        if r.get("status") in ("WIN", "LOSS"):
            try:
                pnls.append(float(r["pnl_pct"]))
            except (ValueError, KeyError):
                pass
    avg_pnl = sum(pnls) / len(pnls) if pnls else 0

    print(f"\n  {'─'*_W}")
    print(f"  SCANNER PICKS  ({total} recommendations)")
    print(f"  {'─'*_W}")
    print(f"  Open: {n_open}  Win: {n_win}  Loss: {n_loss}")
    print(f"  Win Rate: {win_rate:.0f}%  (of {closed} closed)  Avg P&L: {avg_pnl:+.1f}%")

    open_scanner = [r for r in scanner_rows if r.get("status") == "OPEN"]
    if open_scanner:
        print(f"\n  OPEN POSITIONS:")
        for r in open_scanner:
            try:
                pnl    = float(r.get("pnl_pct") or 0)
                usd    = float(r.get("current_price") or 0)
                eur_raw = r.get("price_eur", "")
                eur    = float(eur_raw) if eur_raw else usd * 0.92
                icon   = "+" if pnl >= 0 else "-"
                print(
                    f"    [{icon}] {r['coin']:8s}  entry ${float(r['entry_price']):.4f}"
                    f"  now {_fmt(eur, usd)}  ({pnl:+.1f}%)  — {r['date']}"
                )
            except (ValueError, KeyError):
                pass

    print(f"  {'─'*_W}")
