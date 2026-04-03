"""
CryptoAdvisor Dashboard — Phase 4
Streamlit dashboard with live data: portfolio, scanner picks, Polymarket, TA, LLM costs.

Run:
    streamlit run dashboard.py
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px

import config

# ── Page config ───────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CryptoAdvisor",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Global custom CSS ─────────────────────────────────────────────────────
st.markdown("""
<style>
    /* Dark background override */
    .stApp { background-color: #0d1117; }

    /* Sidebar */
    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #161b22 0%, #0d1117 100%);
        border-right: 1px solid #21262d;
    }

    /* Metric cards */
    div[data-testid="metric-container"] {
        background: #161b22;
        border: 1px solid #21262d;
        border-radius: 12px;
        padding: 16px 20px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.4);
    }
    div[data-testid="metric-container"] label { color: #8b949e !important; font-size: 0.78rem; }
    div[data-testid="metric-container"] div[data-testid="stMetricValue"] {
        color: #e6edf3 !important; font-size: 1.6rem; font-weight: 700;
    }

    /* Section headers */
    h1 { color: #e6edf3 !important; font-size: 1.8rem !important; }
    h2, h3 { color: #c9d1d9 !important; }

    /* Table */
    .dataframe { background: #161b22 !important; color: #e6edf3 !important; }

    /* Card style for info boxes */
    .crypto-card {
        background: #161b22;
        border: 1px solid #21262d;
        border-radius: 10px;
        padding: 14px 18px;
        margin-bottom: 10px;
        box-shadow: 0 1px 4px rgba(0,0,0,0.3);
    }
    .crypto-card b { color: #58a6ff; }

    /* Positive / Negative */
    .pos { color: #3fb950; font-weight: 600; }
    .neg { color: #f85149; font-weight: 600; }
    .neutral { color: #8b949e; }

    /* Signal badge */
    .badge-buy  { background:#0d4429; color:#3fb950; border-radius:6px; padding:2px 10px; font-weight:700; }
    .badge-sell { background:#3d0f0f; color:#f85149; border-radius:6px; padding:2px 10px; font-weight:700; }
    .badge-hold { background:#2d2a00; color:#d29922; border-radius:6px; padding:2px 10px; font-weight:700; }
    .badge-open { background:#0c2d48; color:#58a6ff; border-radius:6px; padding:2px 10px; font-weight:700; }
    .badge-win  { background:#0d4429; color:#3fb950; border-radius:6px; padding:2px 10px; font-weight:700; }
    .badge-loss { background:#3d0f0f; color:#f85149; border-radius:6px; padding:2px 10px; font-weight:700; }

    /* Hide streamlit branding */
    #MainMenu, footer { visibility: hidden; }
    header[data-testid="stHeader"] { background: transparent; }
</style>
""", unsafe_allow_html=True)

# ── Plotly dark theme ─────────────────────────────────────────────────────
PLOT_LAYOUT = dict(
    paper_bgcolor="#0d1117",
    plot_bgcolor="#161b22",
    font_color="#c9d1d9",
    xaxis=dict(gridcolor="#21262d", linecolor="#30363d"),
    yaxis=dict(gridcolor="#21262d", linecolor="#30363d"),
    margin=dict(t=30, b=10, l=10, r=10),
)


# ── Sidebar ───────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 📈 CryptoAdvisor")
    st.caption(f"Updated: {datetime.now().strftime('%H:%M:%S')}")
    st.markdown("---")
    PAGES = {
        "📊 Overview":       "overview",
        "💼 Portfolio":      "portfolio",
        "🎯 Scanner Picks":  "scanner",
        "📰 All Signals":    "signals",
        "🔮 Polymarket":     "polymarket",
        "💸 LLM Costs":      "costs",
    }
    page = st.radio("", list(PAGES.keys()), label_visibility="collapsed")
    st.markdown("---")
    if st.button("🔄 Refresh Data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()


# ── Data loaders ──────────────────────────────────────────────────────────

@st.cache_data(ttl=120)
def load_recommendations() -> pd.DataFrame:
    log_path = config.DATA_DIR / "recommendations.csv"
    if not log_path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(log_path)
        for col in ["entry_price", "stop_loss", "take_profit", "current_price",
                    "price_eur", "pnl_pct", "fear_greed"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=120)
def load_price_history() -> pd.DataFrame:
    hist_path = config.DATA_DIR / "price_history.csv"
    if not hist_path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(hist_path)
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df["price_eur"] = pd.to_numeric(df["price_eur"], errors="coerce")
        df["price_usd"] = pd.to_numeric(df["price_usd"], errors="coerce")
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_live_prices() -> dict:
    try:
        from src.connectors.coingecko import fetch_prices
        pf = json.load(open(config.PORTFOLIO_PATH))
        coin_ids = list({h["coin_id"] for h in pf.get("holdings", []) if h.get("coin_id")})
        coin_ids += [c for c in config.WATCHLIST if c not in coin_ids]
        return {p.coin_id: p for p in fetch_prices(coin_ids)} if coin_ids else {}
    except Exception:
        return {}


@st.cache_data(ttl=300)
def load_fear_greed() -> dict:
    try:
        from src.connectors.coingecko import fetch_fear_greed
        return fetch_fear_greed()
    except Exception:
        return {"value": 50, "label": "Neutral"}


@st.cache_data(ttl=300)
def load_polymarket() -> list[dict]:
    try:
        from src.connectors.polymarket import fetch_crypto_markets
        return fetch_crypto_markets(limit=15)
    except Exception:
        return []


@st.cache_data(ttl=60)
def load_llm_stats() -> pd.DataFrame:
    db_path = config.DATA_DIR / "llm_calls.db"
    if not db_path.exists():
        return pd.DataFrame()
    try:
        con = sqlite3.connect(db_path)
        df = pd.read_sql_query(
            "SELECT date(ts) as date, model, COUNT(*) as calls, "
            "SUM(tokens_in) as tokens_in, SUM(tokens_out) as tokens_out, "
            "SUM(cost_usd) as cost_usd "
            "FROM llm_calls GROUP BY date(ts), model ORDER BY date(ts) DESC",
            con,
        )
        con.close()
        return df
    except Exception:
        return pd.DataFrame()


def load_portfolio() -> dict:
    try:
        return json.load(open(config.PORTFOLIO_PATH))
    except Exception:
        return {"holdings": []}


def _price_fmt(val: float, decimals: int | None = None) -> str:
    if decimals is None:
        decimals = 2 if val >= 1 else 4
    return f"€{val:.{decimals}f}"


def _pnl_html(pct: float | None) -> str:
    if pct is None:
        return '<span class="neutral">—</span>'
    cls = "pos" if pct > 0 else "neg" if pct < 0 else "neutral"
    return f'<span class="{cls}">{pct:+.2f}%</span>'


def _status_badge(status: str) -> str:
    s = str(status).upper()
    cls_map = {"OPEN": "badge-open", "WIN": "badge-win", "LOSS": "badge-loss"}
    cls = cls_map.get(s, "badge-hold")
    return f'<span class="{cls}">{s}</span>'


# ═════════════════════════════════════════════════════════════════════════
# PAGE: OVERVIEW
# ═════════════════════════════════════════════════════════════════════════
if page == "📊 Overview":
    st.markdown("# 📊 Overview")

    prices = load_live_prices()
    pf     = load_portfolio()
    fg     = load_fear_greed()
    df     = load_recommendations()

    # ── KPI row ──────────────────────────────────────────────────────────
    total_eur = sum(
        h["amount"] * prices[h["coin_id"]].price_eur
        for h in pf.get("holdings", [])
        if h.get("coin_id") in prices
    )
    scanner_df = df[df.get("type", pd.Series(dtype=str)).isin(["SCANNER", ""])] if not df.empty and "type" in df.columns else df
    open_picks = len(scanner_df[scanner_df["status"] == "OPEN"]) if not scanner_df.empty else 0
    wins  = len(scanner_df[scanner_df["status"] == "WIN"])  if not scanner_df.empty else 0
    losses= len(scanner_df[scanner_df["status"] == "LOSS"]) if not scanner_df.empty else 0
    wr    = f"{wins/(wins+losses)*100:.0f}%" if (wins+losses) > 0 else "—"

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("💰 Portfolio", f"€{total_eur:.2f}")
    col2.metric("😨 Fear & Greed", f"{fg['value']}/100", fg["label"])
    col3.metric("📂 Open Picks", open_picks)
    col4.metric("🏆 Win Rate", wr, f"{wins}W / {losses}L")
    poly = load_polymarket()
    col5.metric("🔮 Polymarket Mkts", len(poly))

    st.markdown("---")

    # ── Two-column layout ─────────────────────────────────────────────────
    left, right = st.columns([3, 2])

    with left:
        st.markdown("### 🪙 Watchlist")
        rows = []
        for cid in config.WATCHLIST:
            p = prices.get(cid)
            if not p:
                continue
            arr = "▲" if p.change_24h > 0 else "▼" if p.change_24h < 0 else "—"
            rows.append({
                "": arr,
                "Symbol": p.symbol,
                "EUR": _price_fmt(p.price_eur),
                "24h": f"{p.change_24h:+.2f}%",
                "7d":  f"{p.change_7d:+.2f}%",
                "MCap": f"€{p.market_cap/1e6:.0f}M",
            })
        if rows:
            wl_df = pd.DataFrame(rows)
            st.dataframe(wl_df, use_container_width=True, hide_index=True)

        # Fear & Greed gauge
        st.markdown("### 📉 Fear & Greed Gauge")
        fig_fg = go.Figure(go.Indicator(
            mode="gauge+number",
            value=fg["value"],
            title={"text": fg["label"], "font": {"color": "#c9d1d9", "size": 14}},
            number={"font": {"color": "#e6edf3", "size": 36}},
            gauge={
                "axis": {"range": [0, 100], "tickcolor": "#8b949e"},
                "bar": {"color": "#58a6ff", "thickness": 0.25},
                "bgcolor": "#161b22",
                "bordercolor": "#21262d",
                "steps": [
                    {"range": [0,  20], "color": "#3d0f0f"},
                    {"range": [20, 40], "color": "#2d1700"},
                    {"range": [40, 60], "color": "#2d2a00"},
                    {"range": [60, 80], "color": "#0d3220"},
                    {"range": [80,100], "color": "#0d4429"},
                ],
            },
        ))
        fig_fg.update_layout(height=220, **PLOT_LAYOUT)
        st.plotly_chart(fig_fg, use_container_width=True)

    with right:
        st.markdown("### 🔮 Polymarket Odds")
        for m in poly[:8]:
            q = m.get("question", "")
            if not q:
                continue
            prob = m.get("probability")
            vol  = m.get("volume_usd", 0)
            prob_str = f"{prob*100:.0f}%" if prob is not None else "?"
            vol_str  = f"${vol/1000:.0f}k" if vol >= 1000 else f"${vol:.0f}"
            if prob is not None:
                icon = "🟢" if prob >= 0.65 else "🔴" if prob < 0.35 else "🟡"
            else:
                icon = "❓"
            st.markdown(
                f'<div class="crypto-card">'
                f'{icon} <b>{prob_str}</b> &nbsp;{q}<br>'
                f'<small style="color:#8b949e">Vol: {vol_str}</small>'
                f'</div>',
                unsafe_allow_html=True,
            )


# ═════════════════════════════════════════════════════════════════════════
# PAGE: PORTFOLIO
# ═════════════════════════════════════════════════════════════════════════
elif page == "💼 Portfolio":
    st.markdown("# 💼 Portfolio")

    pf     = load_portfolio()
    prices = load_live_prices()
    hist   = load_price_history()

    holdings = pf.get("holdings", [])
    rows = []
    for h in holdings:
        cid = h.get("coin_id", "")
        p   = prices.get(cid)
        if not p:
            continue
        amt     = h["amount"]
        eur_val = amt * p.price_eur
        if eur_val < 0.10:
            continue
        entry   = h.get("entry_price_usd")
        pnl_pct = ((p.price_usd - entry) / entry * 100) if entry else None
        rows.append({
            "Asset":     h["asset"],
            "Amount":    amt,
            "Price":     p.price_eur,
            "Value (€)": round(eur_val, 2),
            "Entry ($)": entry,
            "P&L %":     round(pnl_pct, 2) if pnl_pct is not None else None,
            "24h %":     round(p.change_24h, 2),
            "7d %":      round(p.change_7d, 2),
        })

    if not rows:
        st.info("No holdings found. Check portfolio.json")
    else:
        port_df    = pd.DataFrame(rows)
        total_eur  = port_df["Value (€)"].sum()

        c1, c2, c3 = st.columns(3)
        c1.metric("Total Value", f"€{total_eur:.2f}")

        # Estimate P&L
        cost_eur = 0.0
        for r in rows:
            entry_usd = r["Entry ($)"]
            if entry_usd:
                p = prices.get(next((h["coin_id"] for h in holdings if h["asset"] == r["Asset"]), ""), None)
                if p and p.price_usd:
                    rate = p.price_eur / p.price_usd
                    cost_eur += r["Amount"] * entry_usd * rate
        total_pnl = total_eur - cost_eur
        pnl_pct_t = (total_pnl / cost_eur * 100) if cost_eur > 0 else 0
        c2.metric("Total P&L", f"€{total_pnl:+.2f}", f"{pnl_pct_t:+.1f}%")
        c3.metric("Positions", len(rows))

        st.markdown("---")

        # Cards for each holding
        cols = st.columns(min(len(rows), 3))
        for i, r in enumerate(rows):
            with cols[i % 3]:
                pnl_html = _pnl_html(r["P&L %"])
                arr_24h  = "▲" if r["24h %"] > 0 else "▼"
                cls_24h  = "pos" if r["24h %"] > 0 else "neg"
                st.markdown(
                    f'<div class="crypto-card">'
                    f'<b style="font-size:1.2rem">{r["Asset"]}</b>'
                    f'<span style="float:right;color:#8b949e">{r["Amount"]:.4f}</span><br>'
                    f'<span style="font-size:1.4rem;color:#e6edf3">{_price_fmt(r["Price"])}</span><br>'
                    f'Value: <b>€{r["Value (€)"]:.2f}</b>&nbsp;&nbsp;'
                    f'P&L: {pnl_html}<br>'
                    f'<span class="{cls_24h}">{arr_24h} {r["24h %"]:+.2f}% (24h)</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        # Allocation pie
        st.markdown("---")
        col_pie, col_tbl = st.columns([1, 1])
        with col_pie:
            st.markdown("### Allocation")
            fig_pie = px.pie(
                port_df, values="Value (€)", names="Asset",
                color_discrete_sequence=["#58a6ff","#3fb950","#f85149","#d29922","#bc8cff","#79c0ff"],
                hole=0.4,
            )
            fig_pie.update_traces(textinfo="label+percent", textfont_color="#e6edf3")
            fig_pie.update_layout(height=320, showlegend=False, **PLOT_LAYOUT)
            st.plotly_chart(fig_pie, use_container_width=True)

        with col_tbl:
            st.markdown("### Holdings Table")
            def _color(val):
                if isinstance(val, float):
                    return "color: #3fb950" if val > 0 else "color: #f85149" if val < 0 else ""
                return ""
            st.dataframe(
                port_df.style.applymap(_color, subset=["P&L %","24h %","7d %"])
                .format({"Amount":".4f","Price":"€{:.4f}","Value (€)":"€{:.2f}",
                         "Entry ($)":"${:.4f}","P&L %":"{:+.2f}%",
                         "24h %":"{:+.2f}%","7d %":"{:+.2f}%"}, na_rep="—"),
                use_container_width=True, hide_index=True,
            )

        # Price history chart
        st.markdown("---")
        st.markdown("### Price History")
        if not hist.empty:
            coins_avail = hist["coin"].unique().tolist()
            sel = st.multiselect("Coins", coins_avail, default=coins_avail[:4])
            if sel:
                fig_hist = go.Figure()
                palette  = ["#58a6ff","#3fb950","#f85149","#d29922","#bc8cff","#79c0ff"]
                for i, coin in enumerate(sel):
                    cd = hist[hist["coin"] == coin].sort_values("timestamp")
                    fig_hist.add_trace(go.Scatter(
                        x=cd["timestamp"], y=cd["price_eur"],
                        name=coin, mode="lines",
                        line=dict(color=palette[i % len(palette)], width=2),
                    ))
                fig_hist.update_layout(height=380, hovermode="x unified",
                                       xaxis_title="", yaxis_title="Price (€)",
                                       **PLOT_LAYOUT)
                st.plotly_chart(fig_hist, use_container_width=True)
        else:
            st.info("No price history yet. Run `python run.py --scan` to start logging.")


# ═════════════════════════════════════════════════════════════════════════
# PAGE: SCANNER PICKS
# ═════════════════════════════════════════════════════════════════════════
elif page == "🎯 Scanner Picks":
    st.markdown("# 🎯 Scanner Picks")

    df = load_recommendations()
    if df.empty:
        st.warning("No data yet. Run `python run.py --scan` first.")
    else:
        scanner_df = df[df.get("type", pd.Series(dtype=str)).isin(["SCANNER",""])] if "type" in df.columns else df.copy()

        n_open  = len(scanner_df[scanner_df["status"] == "OPEN"])
        n_win   = len(scanner_df[scanner_df["status"] == "WIN"])
        n_loss  = len(scanner_df[scanner_df["status"] == "LOSS"])
        closed  = n_win + n_loss
        win_rate= (n_win / closed * 100) if closed else 0
        closed_df = scanner_df[scanner_df["status"].isin(["WIN","LOSS"])]
        avg_pnl = closed_df["pnl_pct"].mean() if not closed_df.empty else 0.0

        c1,c2,c3,c4,c5 = st.columns(5)
        c1.metric("Total Picks", len(scanner_df))
        c2.metric("Open", n_open)
        c3.metric("Wins", n_win)
        c4.metric("Losses", n_loss)
        c5.metric("Win Rate", f"{win_rate:.0f}%", f"avg {avg_pnl:+.1f}%")

        st.markdown("---")

        # Open positions as cards
        open_df = scanner_df[scanner_df["status"] == "OPEN"]
        if not open_df.empty:
            st.markdown("### 🔓 Open Positions")
            for _, row in open_df.iterrows():
                pnl = row.get("pnl_pct", None)
                entry = row.get("entry_price", None)
                curr  = row.get("current_price", None)
                sl    = row.get("stop_loss", None)
                tp    = row.get("take_profit", None)

                # Progress toward TP/SL
                bar_html = ""
                if entry and sl and tp and curr:
                    try:
                        rng   = float(tp) - float(sl)
                        pos   = (float(curr) - float(sl)) / rng * 100
                        pos   = max(0, min(100, pos))
                        color = "#3fb950" if pos > 50 else "#f85149"
                        bar_html = (
                            f'<div style="background:#21262d;border-radius:4px;height:6px;margin:6px 0">'
                            f'<div style="width:{pos:.0f}%;background:{color};height:6px;border-radius:4px"></div>'
                            f'</div><small style="color:#8b949e">SL ${float(sl):.4f} → TP ${float(tp):.4f}</small>'
                        )
                    except Exception:
                        pass

                pnl_html = _pnl_html(pnl)
                date_str = str(row.get("date",""))[:16]
                st.markdown(
                    f'<div class="crypto-card">'
                    f'<b style="font-size:1.1rem;color:#58a6ff">{row.get("coin","?")}</b>'
                    f'<span style="float:right">{_status_badge("OPEN")}</span>'
                    f'<br>Entry: <b>${float(entry):.4f}</b> &nbsp;→&nbsp; Now: <b>${float(curr):.4f}</b>'
                    f'&nbsp;&nbsp;P&L: {pnl_html}'
                    f'{bar_html}'
                    f'<br><small style="color:#8b949e">{date_str} &nbsp;|&nbsp; {row.get("timeframe","")}</small>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        st.markdown("---")

        # P&L distribution
        if closed > 0:
            st.markdown("### 📊 P&L Distribution (closed)")
            fig_hist2 = go.Figure()
            wins_data  = closed_df[closed_df["status"]=="WIN"]["pnl_pct"].dropna()
            losses_data= closed_df[closed_df["status"]=="LOSS"]["pnl_pct"].dropna()
            if not wins_data.empty:
                fig_hist2.add_trace(go.Histogram(x=wins_data, name="Win", marker_color="#3fb950", nbinsx=15))
            if not losses_data.empty:
                fig_hist2.add_trace(go.Histogram(x=losses_data, name="Loss", marker_color="#f85149", nbinsx=15))
            fig_hist2.update_layout(barmode="overlay", height=280,
                                    xaxis_title="P&L %", **PLOT_LAYOUT)
            st.plotly_chart(fig_hist2, use_container_width=True)


# ═════════════════════════════════════════════════════════════════════════
# PAGE: ALL SIGNALS
# ═════════════════════════════════════════════════════════════════════════
elif page == "📰 All Signals":
    st.markdown("# 📰 All Signals & Log")

    df = load_recommendations()
    if df.empty:
        st.info("No signals logged yet.")
    else:
        col1, col2, col3 = st.columns(3)
        types    = df["type"].dropna().unique().tolist() if "type" in df.columns else []
        statuses = df["status"].dropna().unique().tolist() if "status" in df.columns else []
        tf = col1.multiselect("Type",   types,    default=types)
        sf = col2.multiselect("Status", statuses, default=statuses)
        cf = col3.text_input("Coin filter", "")

        filtered = df.copy()
        if tf and "type" in filtered.columns:
            filtered = filtered[filtered["type"].isin(tf)]
        if sf and "status" in filtered.columns:
            filtered = filtered[filtered["status"].isin(sf)]
        if cf:
            filtered = filtered[filtered["coin"].str.upper().str.contains(cf.upper(), na=False)]

        st.caption(f"{len(filtered)} of {len(df)} rows")
        st.dataframe(filtered.sort_values("date", ascending=False),
                     use_container_width=True, hide_index=True)


# ═════════════════════════════════════════════════════════════════════════
# PAGE: POLYMARKET
# ═════════════════════════════════════════════════════════════════════════
elif page == "🔮 Polymarket":
    st.markdown("# 🔮 Polymarket Prediction Markets")

    poly = load_polymarket()
    if not poly:
        st.warning("No Polymarket data. Check your connection.")
    else:
        # Sort by volume
        poly_sorted = sorted(poly, key=lambda m: m.get("volume_usd", 0), reverse=True)

        # Summary
        high_confidence = [m for m in poly_sorted if m.get("probability", 0.5) >= 0.7
                           or m.get("probability", 0.5) <= 0.3]
        st.metric("High-Conviction Markets", len(high_confidence))
        st.markdown("---")

        # Cards grid
        cols = st.columns(2)
        for i, m in enumerate(poly_sorted):
            q = m.get("question","")
            if not q:
                continue
            prob = m.get("probability")
            vol  = m.get("volume_usd", 0)
            prob_pct = f"{prob*100:.0f}%" if prob is not None else "?"
            vol_str  = f"${vol/1000:.0f}k" if vol >= 1000 else f"${vol:.0f}"

            # Color the prob bar
            if prob is not None:
                bar_pct = int(prob * 100)
                bar_color = "#3fb950" if prob >= 0.6 else "#f85149" if prob < 0.4 else "#d29922"
            else:
                bar_pct = 50
                bar_color = "#8b949e"

            bar_html = (
                f'<div style="background:#21262d;border-radius:4px;height:8px;margin:8px 0">'
                f'<div style="width:{bar_pct}%;background:{bar_color};height:8px;border-radius:4px"></div>'
                f'</div>'
            )
            with cols[i % 2]:
                st.markdown(
                    f'<div class="crypto-card">'
                    f'<b style="color:#e6edf3">{q}</b><br>'
                    f'{bar_html}'
                    f'<span style="font-size:1.4rem;color:{bar_color};font-weight:700">{prob_pct}</span>'
                    f'&nbsp;&nbsp;<small style="color:#8b949e">Vol: {vol_str}</small>'
                    f'</div>',
                    unsafe_allow_html=True,
                )


# ═════════════════════════════════════════════════════════════════════════
# PAGE: LLM COSTS
# ═════════════════════════════════════════════════════════════════════════
elif page == "💸 LLM Costs":
    st.markdown("# 💸 LLM Usage & Budget")

    llm_df = load_llm_stats()
    today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if llm_df.empty:
        st.info("No LLM calls logged yet. Run `python run.py --scan` to start.")
    else:
        today_df = llm_df[llm_df["date"] == today]
        today_calls = int(today_df["calls"].sum()) if not today_df.empty else 0
        today_cost  = float(today_df["cost_usd"].sum()) if not today_df.empty else 0.0
        today_tok   = int((today_df.get("tokens_in", pd.Series([0])).fillna(0) +
                           today_df.get("tokens_out", pd.Series([0])).fillna(0)).sum()) if not today_df.empty else 0

        c1, c2, c3 = st.columns(3)
        c1.metric("Today's Calls", today_calls)
        c2.metric("Today's Tokens", f"{today_tok:,}")
        c3.metric("Today's Cost", f"${today_cost:.4f}")

        st.markdown("---")
        st.markdown("### Daily Budget Status")
        for model, limits in config.DAILY_BUDGET_LIMITS.items():
            model_today = today_df[today_df["model"] == model] if not today_df.empty else pd.DataFrame()
            calls_used  = int(model_today["calls"].sum()) if not model_today.empty else 0
            max_calls   = limits.get("max_calls", 0)
            if max_calls:
                pct = min(calls_used / max_calls, 1.0)
                color = "#3fb950" if pct < 0.5 else "#d29922" if pct < 0.8 else "#f85149"
                st.markdown(
                    f'<div class="crypto-card">'
                    f'<b>{model}</b>: {calls_used}/{max_calls} calls'
                    f'<div style="background:#21262d;border-radius:4px;height:8px;margin:8px 0">'
                    f'<div style="width:{pct*100:.0f}%;background:{color};height:8px;border-radius:4px"></div>'
                    f'</div>'
                    f'<small style="color:#8b949e">{pct*100:.0f}% of daily limit</small>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        st.markdown("---")
        st.markdown("### Calls per Day")
        daily_agg = llm_df.groupby("date")["calls"].sum().reset_index()
        fig_calls = go.Figure(go.Bar(
            x=daily_agg["date"], y=daily_agg["calls"],
            marker_color="#58a6ff",
        ))
        fig_calls.update_layout(height=300, xaxis_title="", yaxis_title="Calls", **PLOT_LAYOUT)
        st.plotly_chart(fig_calls, use_container_width=True)

        with st.expander("Full LLM Log"):
            st.dataframe(llm_df, use_container_width=True)
