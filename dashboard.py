"""
CryptoAdvisor Dashboard — Frutiger Aero Edition
Streamlit dashboard: portfolio, scanner picks, TA, charts, Polymarket, LLM costs.

Run:
    streamlit run dashboard.py
"""
import json
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px

import config

BASE_DIR = Path(__file__).parent

# ── Page config ───────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CryptoAdvisor",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Frutiger Aero CSS ─────────────────────────────────────────────────────
st.markdown("""
<style>
/* ═══════════════ BACKGROUND ═══════════════ */
.stApp {
    background:
        radial-gradient(ellipse at 20% 20%, rgba(0,120,200,0.18) 0%, transparent 55%),
        radial-gradient(ellipse at 80% 80%, rgba(0,200,180,0.12) 0%, transparent 55%),
        radial-gradient(ellipse at 60% 10%, rgba(80,160,255,0.10) 0%, transparent 45%),
        linear-gradient(160deg, #020d1e 0%, #071a3e 25%, #0b3060 50%, #0d4a7a 70%, #0e6090 100%);
    background-attachment: fixed;
    min-height: 100vh;
}

/* ═══════════════ SIDEBAR ═══════════════ */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg,
        rgba(2,10,28,0.96) 0%,
        rgba(5,20,50,0.94) 60%,
        rgba(8,35,70,0.92) 100%) !important;
    border-right: 1px solid rgba(100,180,255,0.12);
    box-shadow: 4px 0 30px rgba(0,0,0,0.5);
}
section[data-testid="stSidebar"] .stMarkdown p,
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] span {
    color: #b0d8f8 !important;
}

/* ═══════════════ METRIC CARDS ═══════════════ */
div[data-testid="metric-container"] {
    background: linear-gradient(145deg,
        rgba(255,255,255,0.11) 0%,
        rgba(100,180,255,0.06) 50%,
        rgba(255,255,255,0.04) 100%);
    border: 1px solid rgba(255,255,255,0.16);
    border-radius: 18px;
    padding: 16px 20px;
    box-shadow:
        0 8px 32px rgba(0,0,0,0.35),
        inset 0 1px 0 rgba(255,255,255,0.22),
        inset 0 -1px 0 rgba(0,0,0,0.15);
    backdrop-filter: blur(12px);
}
div[data-testid="metric-container"] label {
    color: #7dd3fc !important;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}
div[data-testid="metric-container"] div[data-testid="stMetricValue"] {
    color: #f0f8ff !important;
    font-size: 1.6rem;
    font-weight: 800;
}
div[data-testid="metric-container"] div[data-testid="stMetricDelta"] {
    color: #5eead4 !important;
}

/* ═══════════════ HEADINGS ═══════════════ */
h1 {
    color: #f0f8ff !important;
    font-size: 1.9rem !important;
    font-weight: 800 !important;
    text-shadow: 0 0 40px rgba(56,189,248,0.45);
    letter-spacing: -0.02em;
}
h2, h3 {
    color: #e0f0ff !important;
    font-weight: 700 !important;
    text-shadow: 0 0 20px rgba(56,189,248,0.25);
}

/* ═══════════════ GLASS CARD ═══════════════ */
.glass {
    background: linear-gradient(145deg,
        rgba(255,255,255,0.10) 0%,
        rgba(100,180,255,0.05) 100%);
    border: 1px solid rgba(255,255,255,0.13);
    border-radius: 18px;
    padding: 18px 20px;
    margin-bottom: 12px;
    box-shadow:
        0 8px 32px rgba(0,0,0,0.40),
        inset 0 1px 0 rgba(255,255,255,0.20),
        inset 0 -1px 0 rgba(0,50,100,0.15);
    backdrop-filter: blur(16px);
    position: relative;
    overflow: hidden;
}
.glass::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 38%;
    background: linear-gradient(180deg,
        rgba(255,255,255,0.08) 0%,
        transparent 100%);
    border-radius: 18px 18px 0 0;
    pointer-events: none;
}
.glass b  { color: #7dd3fc; }
.glass small { color: #94b8d8; }

/* ═══════════════ BUTTONS ═══════════════ */
.stButton > button {
    background: linear-gradient(180deg,
        rgba(255,255,255,0.18) 0%,
        rgba(100,180,255,0.10) 50%,
        rgba(0,100,200,0.20) 100%) !important;
    border: 1px solid rgba(255,255,255,0.22) !important;
    border-radius: 28px !important;
    color: #e8f4ff !important;
    font-weight: 600 !important;
    letter-spacing: 0.02em;
    box-shadow:
        0 4px 16px rgba(0,120,255,0.22),
        inset 0 1px 0 rgba(255,255,255,0.28) !important;
    transition: all 0.18s ease !important;
}
.stButton > button:hover {
    box-shadow:
        0 6px 28px rgba(0,160,255,0.40),
        inset 0 1px 0 rgba(255,255,255,0.36) !important;
    border-color: rgba(255,255,255,0.35) !important;
    transform: translateY(-2px);
}
.stButton > button:active { transform: translateY(0); }

/* ═══════════════ INPUTS ═══════════════ */
.stSelectbox > div > div,
.stTextInput > div > div > input,
.stMultiSelect > div > div {
    background: rgba(255,255,255,0.07) !important;
    border: 1px solid rgba(255,255,255,0.14) !important;
    border-radius: 10px !important;
    color: #d0e8ff !important;
}
.stSelectbox label, .stTextInput label, .stMultiSelect label {
    color: #7dd3fc !important;
    font-size: 0.8rem;
    font-weight: 600;
}

/* ═══════════════ RADIO ═══════════════ */
.stRadio label { color: #b0d8f8 !important; }
.stRadio [data-testid="stMarkdownContainer"] p { color: #b0d8f8 !important; }

/* ═══════════════ TOGGLE ═══════════════ */
.stToggle label span { color: #b0d8f8 !important; }
.stToggle p { color: #b0d8f8 !important; }

/* ═══════════════ CHECKBOX ═══════════════ */
.stCheckbox label span { color: #b0d8f8 !important; }

/* ═══════════════ TABS ═══════════════ */
.stTabs [data-baseweb="tab-list"] {
    background: rgba(255,255,255,0.05);
    border-radius: 12px;
    padding: 4px;
    gap: 4px;
}
.stTabs [data-baseweb="tab"] {
    border-radius: 9px;
    color: #7dd3fc !important;
    font-weight: 600;
}
.stTabs [aria-selected="true"] {
    background: rgba(255,255,255,0.12) !important;
    color: #f0f8ff !important;
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.20);
}

/* ═══════════════ DATAFRAME ═══════════════ */
.stDataFrame {
    border-radius: 14px;
    overflow: hidden;
    border: 1px solid rgba(255,255,255,0.10);
}
iframe[title="st_aggrid"] { border-radius: 14px; }

/* ═══════════════ EXPANDER ═══════════════ */
details summary {
    color: #7dd3fc !important;
    font-weight: 600;
}
.streamlit-expanderHeader {
    background: rgba(255,255,255,0.06) !important;
    border-radius: 10px !important;
    color: #7dd3fc !important;
}

/* ═══════════════ SPINNER / INFO / WARNING ═══════════════ */
.stSpinner { color: #38bdf8; }
.stAlert {
    background: rgba(255,255,255,0.07) !important;
    border: 1px solid rgba(255,255,255,0.12) !important;
    border-radius: 12px !important;
    color: #d0e8ff !important;
}
div[data-testid="stStatusWidget"] { color: #38bdf8 !important; }

/* ═══════════════ DIVIDER ═══════════════ */
hr { border-color: rgba(255,255,255,0.08) !important; margin: 18px 0; }

/* ═══════════════ BADGES ═══════════════ */
.badge-open {
    background: linear-gradient(135deg,rgba(56,189,248,0.25),rgba(14,116,144,0.20));
    color: #7dd3fc; border: 1px solid rgba(56,189,248,0.30);
    border-radius: 20px; padding: 3px 12px; font-weight: 700; font-size: 0.76rem;
}
.badge-win {
    background: linear-gradient(135deg,rgba(52,211,153,0.25),rgba(6,95,70,0.20));
    color: #6ee7b7; border: 1px solid rgba(52,211,153,0.30);
    border-radius: 20px; padding: 3px 12px; font-weight: 700; font-size: 0.76rem;
}
.badge-loss {
    background: linear-gradient(135deg,rgba(248,113,113,0.25),rgba(153,27,27,0.20));
    color: #fca5a5; border: 1px solid rgba(248,113,113,0.30);
    border-radius: 20px; padding: 3px 12px; font-weight: 700; font-size: 0.76rem;
}
.badge-paper {
    background: linear-gradient(135deg,rgba(251,191,36,0.25),rgba(146,64,14,0.20));
    color: #fde68a; border: 1px solid rgba(251,191,36,0.30);
    border-radius: 20px; padding: 3px 12px; font-weight: 700; font-size: 0.76rem;
}
.badge-buy {
    background: linear-gradient(135deg,rgba(52,211,153,0.25),rgba(6,95,70,0.20));
    color: #6ee7b7; border: 1px solid rgba(52,211,153,0.30);
    border-radius: 20px; padding: 3px 12px; font-weight: 700; font-size: 0.76rem;
}
.badge-neutral {
    background: rgba(255,255,255,0.08);
    color: #94b8d8; border: 1px solid rgba(255,255,255,0.12);
    border-radius: 20px; padding: 3px 12px; font-weight: 700; font-size: 0.76rem;
}

/* ═══════════════ PNL COLORS ═══════════════ */
.pos     { color: #6ee7b7; font-weight: 700; }
.neg     { color: #fca5a5; font-weight: 700; }
.neutral { color: #94b8d8; }

/* ═══════════════ PROGRESS BAR ═══════════════ */
.aero-track {
    background: rgba(255,255,255,0.08);
    border-radius: 8px; height: 7px; margin: 7px 0;
    border: 1px solid rgba(255,255,255,0.06); overflow: hidden;
}
.aero-fill-green { background:linear-gradient(90deg,#22c55e,#86efac); height:7px; border-radius:8px; box-shadow:0 0 8px rgba(34,197,94,0.55); }
.aero-fill-red   { background:linear-gradient(90deg,#ef4444,#fca5a5); height:7px; border-radius:8px; box-shadow:0 0 8px rgba(239,68,68,0.55); }
.aero-fill-blue  { background:linear-gradient(90deg,#3b82f6,#93c5fd); height:7px; border-radius:8px; box-shadow:0 0 8px rgba(59,130,246,0.55); }
.aero-fill-yellow{ background:linear-gradient(90deg,#f59e0b,#fde68a); height:7px; border-radius:8px; box-shadow:0 0 8px rgba(245,158,11,0.55); }

/* ═══════════════ TERMINAL OUTPUT ═══════════════ */
.terminal {
    background: rgba(0,8,20,0.88);
    border: 1px solid rgba(56,189,248,0.18);
    border-radius: 12px; padding: 14px 16px;
    font-family: 'Courier New', monospace; font-size: 0.76rem;
    color: #7dd3fc; max-height: 420px; overflow-y: auto;
    white-space: pre-wrap; line-height: 1.55;
    box-shadow: inset 0 0 24px rgba(0,0,0,0.5), 0 0 20px rgba(56,189,248,0.06);
}

/* ═══════════════ HIDE CHROME ═══════════════ */
#MainMenu, footer { visibility: hidden; }
header[data-testid="stHeader"] { background: transparent; }
</style>
""", unsafe_allow_html=True)

# ── Plotly Aero theme ─────────────────────────────────────────────────────
PLOT_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(255,255,255,0.03)",
    font_color="#b0d8f8",
    font_family="system-ui, -apple-system, sans-serif",
    xaxis=dict(gridcolor="rgba(255,255,255,0.06)", linecolor="rgba(255,255,255,0.10)",
               tickfont=dict(color="#7dd3fc"), zerolinecolor="rgba(255,255,255,0.06)"),
    yaxis=dict(gridcolor="rgba(255,255,255,0.06)", linecolor="rgba(255,255,255,0.10)",
               tickfont=dict(color="#7dd3fc"), zerolinecolor="rgba(255,255,255,0.06)"),
    margin=dict(t=36, b=12, l=8, r=8),
    legend=dict(bgcolor="rgba(0,0,0,0.3)", bordercolor="rgba(255,255,255,0.10)", borderwidth=1),
)
AERO_PALETTE = ["#38bdf8","#34d399","#f87171","#fbbf24","#a78bfa","#22d3ee","#fb923c","#f472b6","#818cf8","#4ade80"]


# ── Session state init ────────────────────────────────────────────────────
for _k, _v in [("scan_output",""),("last_scan_ts",None),("auto_scan",False),("sched_alive",False)]:
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ── Helpers ───────────────────────────────────────────────────────────────
def _run_scan(exchange: str | None, debate: bool, stocks_only: bool = False) -> str:
    if stocks_only:
        cmd = [sys.executable, str(BASE_DIR / "run.py"), "--stocks"]
    else:
        cmd = [sys.executable, str(BASE_DIR / "run.py"), "--scan"]
        if exchange:
            cmd += ["--exchange", exchange]
        if debate:
            cmd += ["--debate"]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=str(BASE_DIR), timeout=900, encoding="utf-8", errors="replace",
        )
        out = (r.stdout or "")
        err = (r.stderr or "").strip()
        # Only show stderr if it's a real error (not just warnings)
        if err and "error" in err.lower():
            out += f"\n\nSTDERR:\n{err}"
        return out or "(no output — check run.py logs)"
    except subprocess.TimeoutExpired:
        return "ERROR: Scan timed out (15 min limit)"
    except Exception as e:
        return f"ERROR launching scan: {e}"


def _sched_loop(exchange, debate, stocks_only=False):
    while st.session_state.get("auto_scan"):
        out = _run_scan(exchange, debate, stocks_only=stocks_only)
        st.session_state["scan_output"] = out
        st.session_state["last_scan_ts"] = datetime.now()
        st.cache_data.clear()
        for _ in range(4 * 360):          # 4 h in 10 s steps
            if not st.session_state.get("auto_scan"):
                break
            time.sleep(10)
    st.session_state["sched_alive"] = False


def _price_fmt(val: float, decimals: int | None = None) -> str:
    if decimals is None:
        decimals = 2 if val >= 1 else 4 if val >= 0.01 else 6 if val >= 0.0001 else 8
    return f"${val:.{decimals}f}"


def _pnl_html(pct: float | None) -> str:
    if pct is None:
        return '<span class="neutral">—</span>'
    cls = "pos" if pct > 0 else "neg" if pct < 0 else "neutral"
    return f'<span class="{cls}">{pct:+.2f}%</span>'


def _status_badge(status: str) -> str:
    s = str(status).upper()
    cls = {"OPEN":"badge-open","WIN":"badge-win","LOSS":"badge-loss"}.get(s,"badge-neutral")
    return f'<span class="{cls}">{s}</span>'


def _trend_badge(trend: str) -> str:
    t = str(trend).upper()
    if t == "BULLISH":
        return '<span class="badge-buy">🟢 BULLISH</span>'
    if t == "BEARISH":
        return '<span class="badge-loss">🔴 BEARISH</span>'
    return '<span class="badge-neutral">⚪ NEUTRAL</span>'


def _aero_bar(pct: float, color: str = "blue") -> str:
    p = max(0, min(100, pct))
    return (
        f'<div class="aero-track">'
        f'<div class="aero-fill-{color}" style="width:{p:.1f}%"></div>'
        f'</div>'
    )


# ── Sidebar ───────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("""
    <div style="text-align:center;padding:14px 0 10px">
        <div style="font-size:2.2rem;filter:drop-shadow(0 0 12px rgba(56,189,248,0.7))">📈</div>
        <div style="font-size:1.05rem;font-weight:800;color:#7dd3fc;letter-spacing:0.04em;margin-top:4px">CryptoAdvisor</div>
        <div style="font-size:0.65rem;color:#4a7a9b;margin-top:2px;letter-spacing:0.06em">FRUTIGER AERO EDITION</div>
    </div>
    """, unsafe_allow_html=True)
    st.markdown(
        f"<div style='text-align:center;color:#3a6a8a;font-size:0.68rem;margin-bottom:8px'>"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}</div>",
        unsafe_allow_html=True,
    )
    st.markdown("---")

    PAGES = {
        "🚀 Run Scanner":        "run",
        "📊 Overview":           "overview",
        "💼 Portfolio":          "portfolio",
        "🎯 Scanner Picks":      "scanner",
        "📐 Technical Analysis": "ta",
        "📈 Charts":             "charts",
        "📰 All Signals":        "signals",
        "🔮 Polymarket Advisor": "polymarket",
        "📊 Stock Scanner":      "stocks",
        "💸 LLM Costs":          "costs",
    }
    page = st.radio("Nav", list(PAGES.keys()), label_visibility="collapsed")

    st.markdown("---")
    st.markdown("<div style='font-size:0.72rem;font-weight:700;color:#38bdf8;letter-spacing:0.08em;margin-bottom:6px'>⚡ SCANNER CONTROLS</div>", unsafe_allow_html=True)

    _exch_map = {"All Exchanges":None,"Binance":"binance","Revolut":"revolut","All (Revolut+Binance)":"all"}
    _exch_label = st.selectbox("Exchange", list(_exch_map.keys()), label_visibility="collapsed")
    _exch_val   = _exch_map[_exch_label]
    _debate_val = st.toggle("🥊 Bull/Bear Debate", value=False)
    _stocks_val = st.toggle("📊 Stocks Only",      value=False)

    _c1, _c2 = st.columns(2)
    if _c1.button("▶ Run Scan", use_container_width=True):
        _label = "stocks-only" if _stocks_val else "full scan (crypto + stocks + Polymarket)"
        with st.spinner(f"Running {_label}… (~2-5 min)"):
            out = _run_scan(_exch_val, _debate_val, stocks_only=_stocks_val)
            st.session_state["scan_output"] = out
            st.session_state["last_scan_ts"] = datetime.now()
            st.cache_data.clear()
        st.rerun()

    _auto = _c2.toggle("⏱ 4h auto", value=st.session_state["auto_scan"])
    if _auto != st.session_state["auto_scan"]:
        st.session_state["auto_scan"] = _auto
        if _auto and not st.session_state["sched_alive"]:
            st.session_state["sched_alive"] = True
            threading.Thread(target=_sched_loop, args=(_exch_val, _debate_val, _stocks_val), daemon=True).start()

    if st.session_state["auto_scan"]:
        st.markdown('<div style="color:#6ee7b7;font-size:0.70rem;text-align:center;margin-top:2px">● Auto-scan active</div>', unsafe_allow_html=True)
    if st.session_state["last_scan_ts"]:
        st.markdown(f'<div style="color:#3a6a8a;font-size:0.67rem;text-align:center">Last: {st.session_state["last_scan_ts"].strftime("%H:%M:%S")}</div>', unsafe_allow_html=True)

    st.markdown("---")
    if st.button("🔄 Refresh Data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()


# ── Data loaders ──────────────────────────────────────────────────────────

@st.cache_data(ttl=120)
def load_recommendations() -> pd.DataFrame:
    path = config.DATA_DIR / "recommendations.csv"
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
        for col in ["entry_price","stop_loss","take_profit","current_price","price_eur","pnl_pct","fear_greed"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=120)
def load_price_history() -> pd.DataFrame:
    path = config.DATA_DIR / "price_history.csv"
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
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
        coin_ids: set[str] = set()
        # Try live Kraken holdings first (source of truth for amounts)
        try:
            from src.connectors.kraken import fetch_kraken_portfolio, _COIN_IDS as _KRK_IDS
            kh, _ = fetch_kraken_portfolio()
            if kh:
                coin_ids.update(h["coin_id"] for h in kh if h.get("coin_id"))
        except Exception:
            pass
        # Fallback: portfolio.json coin_ids
        try:
            pf = json.load(open(config.PORTFOLIO_PATH))
            coin_ids.update(h["coin_id"] for h in pf.get("holdings",[]) if h.get("coin_id"))
        except Exception:
            pass
        coin_ids.update(config.WATCHLIST)
        try:
            rdf = pd.read_csv(config.DATA_DIR/"recommendations.csv")
            mask = (rdf.get("type",pd.Series(dtype=str)).isin(["SCANNER",""])) & (rdf["status"]=="OPEN")
            coin_ids.update(c for c in rdf.loc[mask,"coin_id"].dropna() if c)
        except Exception:
            pass
        return {p.coin_id: p for p in fetch_prices(list(coin_ids))} if coin_ids else {}
    except Exception:
        return {}


@st.cache_data(ttl=300)
def load_fear_greed() -> dict:
    try:
        from src.connectors.coingecko import fetch_fear_greed
        return fetch_fear_greed()
    except Exception:
        return {"value":50,"label":"Neutral"}


@st.cache_data(ttl=300)
def load_polymarket() -> list[dict]:
    try:
        from src.connectors.polymarket import fetch_crypto_markets
        return fetch_crypto_markets(limit=15)
    except Exception:
        return []


@st.cache_data(ttl=60)
def load_llm_stats() -> pd.DataFrame:
    db = config.DATA_DIR / "llm_calls.db"
    if not db.exists():
        return pd.DataFrame()
    try:
        con = sqlite3.connect(db)
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


def load_portfolio_json() -> dict:
    try:
        return json.load(open(config.PORTFOLIO_PATH))
    except Exception:
        return {"holdings":[]}


@st.cache_data(ttl=60)
def load_kraken_holdings() -> tuple[list[dict], str]:
    try:
        from src.connectors.kraken import fetch_kraken_portfolio
        h, src = fetch_kraken_portfolio()
        if h:
            return h, src
    except Exception:
        pass
    pf = load_portfolio_json()
    return pf.get("holdings",[]), "portfolio.json"


@st.cache_data(ttl=120)
def load_polymarket_picks() -> pd.DataFrame:
    path = config.DATA_DIR / "polymarket_picks.csv"
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
        for col in ["current_odds_pct","llm_confidence_pct","llm_edge_pct"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=120)
def load_stock_recommendations() -> pd.DataFrame:
    path = config.DATA_DIR / "stock_recommendations.csv"
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
        for col in ["entry_price","stop_loss","take_profit","current_price","pnl_pct","pe_ratio"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def load_technical_analysis() -> dict:
    try:
        from src.connectors.coingecko import fetch_ohlcv
        from src.agents.technical_analyst import compute_ta
        results = {}
        for coin_id in config.WATCHLIST:
            symbol = config.WATCHLIST_SYMBOLS.get(coin_id, coin_id.upper())
            try:
                ohlcv = fetch_ohlcv(coin_id, days=30)
                if ohlcv and len(ohlcv) >= 14:
                    results[coin_id] = compute_ta(coin_id, symbol, ohlcv)
            except Exception:
                pass
        return results
    except Exception:
        return {}


# ═════════════════════════════════════════════════════════════════════════
# PAGE: RUN SCANNER
# ═════════════════════════════════════════════════════════════════════════
if page == "🚀 Run Scanner":
    st.markdown("# 🚀 Run Scanner")

    # ── Controls ──────────────────────────────────────────────────────────
    col_a, col_b = st.columns([2, 2])
    with col_a:
        exch_map = {
            "All Exchanges":        None,
            "Binance":              "binance",
            "Revolut X":            "revolut",
            "All (Revolut+Binance)":"all",
        }
        exch_label = st.selectbox("Exchange", list(exch_map.keys()), label_visibility="visible")
        exch_val   = exch_map[exch_label]
        debate_val  = st.toggle("🥊 Bull/Bear Debate", value=False, key="run_debate")
        stocks_only = st.toggle("📊 Stocks Only",      value=False, key="run_stocks")
    with col_b:
        st.markdown("<br><br>", unsafe_allow_html=True)
        run_btn = st.button("▶  Run Scan", use_container_width=True, type="primary")
        auto_val = st.toggle("⏱ Auto every 4h", value=st.session_state["auto_scan"], key="run_auto")
        if auto_val != st.session_state["auto_scan"]:
            st.session_state["auto_scan"] = auto_val
            if auto_val and not st.session_state["sched_alive"]:
                st.session_state["sched_alive"] = True
                threading.Thread(target=_sched_loop, args=(exch_val, debate_val, stocks_only), daemon=True).start()
        if st.session_state["auto_scan"]:
            st.markdown('<span style="color:#6ee7b7;font-size:0.8rem">● Auto-scan active</span>', unsafe_allow_html=True)

    st.markdown("---")

    # ── Execute ───────────────────────────────────────────────────────────
    if run_btn:
        label = "Stocks only" if stocks_only else "Full scan (crypto + stocks + Polymarket)"
        with st.spinner(f"Running {label}… (~2-5 min)"):
            out = _run_scan(exch_val, debate_val, stocks_only=stocks_only)
            st.session_state["scan_output"] = out
            st.session_state["last_scan_ts"] = datetime.now()
            st.cache_data.clear()
        st.rerun()

    # ── Output ────────────────────────────────────────────────────────────
    if st.session_state["scan_output"]:
        ts_str = st.session_state["last_scan_ts"].strftime("%d %b %H:%M:%S") if st.session_state["last_scan_ts"] else ""
        st.markdown(f"**📟 Output** <small style='color:#3a6a8a'>{ts_str}</small>", unsafe_allow_html=True)
        st.code(st.session_state["scan_output"], language=None)
        if st.button("🗑 Clear Output", key="run_clear"):
            st.session_state["scan_output"] = ""
            st.rerun()
    else:
        st.info("No scan output yet — press ▶ Run Scan above.")


# ═════════════════════════════════════════════════════════════════════════
# PAGE: OVERVIEW
# ═════════════════════════════════════════════════════════════════════════
elif page == "📊 Overview":
    st.markdown("# 📊 Overview")

    prices          = load_live_prices()
    holdings, _src  = load_kraken_holdings()
    fg              = load_fear_greed()
    df              = load_recommendations()
    poly            = load_polymarket()

    # KPI row
    total_usd = sum(
        h["amount"] * prices[h["coin_id"]].price_usd
        for h in holdings if h.get("coin_id") in prices
    )
    scanner_df = df[df.get("type",pd.Series(dtype=str)).isin(["SCANNER",""])] if not df.empty and "type" in df.columns else df
    n_open  = len(scanner_df[scanner_df["status"]=="OPEN"]) if not scanner_df.empty else 0
    n_win   = len(scanner_df[scanner_df["status"]=="WIN"])  if not scanner_df.empty else 0
    n_loss  = len(scanner_df[scanner_df["status"]=="LOSS"]) if not scanner_df.empty else 0
    wr      = f"{n_win/(n_win+n_loss)*100:.0f}%" if (n_win+n_loss)>0 else "—"

    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("💰 Portfolio",      f"${total_usd:,.2f}")
    c2.metric("😨 Fear & Greed",   f"{fg['value']}/100", fg["label"])
    c3.metric("📂 Open Picks",     n_open)
    c4.metric("🏆 Win Rate",       wr, f"{n_win}W / {n_loss}L")
    c5.metric("🔮 Polymarket Mkts",len(poly))

    st.markdown("---")
    left, right = st.columns([3,2])

    with left:
        # Watchlist table
        st.markdown("### 🪙 Watchlist Prices")
        rows = []
        for cid in config.WATCHLIST:
            p = prices.get(cid)
            if not p:
                continue
            arr = "▲" if p.change_24h > 0 else "▼" if p.change_24h < 0 else "—"
            decimals = 2 if p.price_usd >= 1 else 4 if p.price_usd >= 0.01 else 6 if p.price_usd >= 0.0001 else 8
            rows.append({
                "":       arr,
                "Symbol": p.symbol,
                "USD":    f"${p.price_usd:.{decimals}f}",
                "24h %":  f"{p.change_24h:+.2f}%",
                "7d %":   f"{p.change_7d:+.2f}%",
                "MCap":   f"${p.market_cap/1e6:.0f}M",
            })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("No live price data — click ▶ Run Scan in the sidebar.")

        # F&G gauge
        st.markdown("### 📉 Fear & Greed")
        fg_val = fg["value"]
        fg_color = "#f87171" if fg_val < 25 else "#fb923c" if fg_val < 40 else "#fbbf24" if fg_val < 60 else "#34d399" if fg_val < 75 else "#22d3ee"
        fig_fg = go.Figure(go.Indicator(
            mode="gauge+number",
            value=fg_val,
            title={"text":fg["label"],"font":{"color":"#7dd3fc","size":13}},
            number={"font":{"color":"#f0f8ff","size":38}},
            gauge={
                "axis":{"range":[0,100],"tickcolor":"#3a6a8a"},
                "bar":{"color":fg_color,"thickness":0.22},
                "bgcolor":"rgba(0,0,0,0)",
                "bordercolor":"rgba(255,255,255,0.08)",
                "steps":[
                    {"range":[0,20],  "color":"rgba(239,68,68,0.20)"},
                    {"range":[20,40], "color":"rgba(249,115,22,0.16)"},
                    {"range":[40,60], "color":"rgba(234,179,8,0.14)"},
                    {"range":[60,80], "color":"rgba(34,197,94,0.14)"},
                    {"range":[80,100],"color":"rgba(34,211,238,0.18)"},
                ],
            },
        ))
        fig_fg.update_layout(height=220, **PLOT_LAYOUT)
        st.plotly_chart(fig_fg, use_container_width=True)

    with right:
        st.markdown("### 🔮 Polymarket Odds")
        for m in poly[:8]:
            q = m.get("question","")
            if not q:
                continue
            prob = m.get("probability")
            vol  = m.get("volume_usd",0)
            prob_str = f"{prob*100:.0f}%" if prob is not None else "?"
            vol_str  = f"${vol/1000:.0f}k" if vol>=1000 else f"${vol:.0f}"
            bar_pct  = int((prob or 0.5)*100)
            bar_col  = "green" if (prob or 0) >= 0.6 else "red" if (prob or 0.5) < 0.4 else "yellow"
            icon     = "🟢" if (prob or 0) >= 0.65 else "🔴" if (prob or 0.5) < 0.35 else "🟡"
            st.markdown(
                f'<div class="glass">'
                f'{icon} <b style="font-size:0.88rem">{prob_str}</b> &nbsp;'
                f'<span style="color:#d0e8ff;font-size:0.82rem">{q}</span><br>'
                f'{_aero_bar(bar_pct, bar_col)}'
                f'<small>Vol: {vol_str}</small>'
                f'</div>',
                unsafe_allow_html=True,
            )

    # Scan output — always shown expanded on Overview if present
    if st.session_state["scan_output"]:
        st.markdown("---")
        ts_str = st.session_state["last_scan_ts"].strftime("%H:%M:%S") if st.session_state["last_scan_ts"] else ""
        with st.expander(f"📟 Last Scan Output  [{ts_str}]", expanded=True):
            st.markdown(f'<div class="terminal">{st.session_state["scan_output"]}</div>', unsafe_allow_html=True)
            if st.button("🗑 Clear Output"):
                st.session_state["scan_output"] = ""
                st.rerun()


# ═════════════════════════════════════════════════════════════════════════
# PAGE: PORTFOLIO
# ═════════════════════════════════════════════════════════════════════════
elif page == "💼 Portfolio":
    st.markdown("# 💼 Portfolio")

    holdings, src = load_kraken_holdings()
    prices        = load_live_prices()
    hist          = load_price_history()

    rows = []
    for h in holdings:
        cid = h.get("coin_id","")
        p   = prices.get(cid)
        if not p:
            continue
        amt     = h["amount"]
        usd_val = amt * p.price_usd
        if usd_val < 0.12:
            continue
        entry_usd = h.get("entry_price_usd")
        pnl_pct = ((p.price_usd - entry_usd)/entry_usd*100) if entry_usd else None
        rows.append({
            "Asset":    h["asset"],
            "Amount":   amt,
            "Price $":  p.price_usd,
            "Value $":  round(usd_val,2),
            "Entry $":  round(entry_usd, 6) if entry_usd else None,
            "P&L %":    round(pnl_pct,2) if pnl_pct is not None else None,
            "24h %":    round(p.change_24h,2),
            "7d %":     round(p.change_7d,2),
            "_coin_id": cid,
        })

    st.caption(f"Source: {src}")
    if not rows:
        st.info("No holdings found. Check Kraken API or portfolio.json.")
    else:
        port_df   = pd.DataFrame(rows)
        total_usd = port_df["Value $"].sum()

        cost_usd = 0.0
        for r in rows:
            ep = r["Entry $"]
            if ep:
                cost_usd += r["Amount"] * ep

        total_pnl     = total_usd - cost_usd
        total_pnl_pct = (total_pnl/cost_usd*100) if cost_usd > 0 else 0

        c1,c2,c3 = st.columns(3)
        c1.metric("Total Value",   f"${total_usd:,.2f}")
        c2.metric("Total P&L",     f"${total_pnl:+,.2f}", f"{total_pnl_pct:+.1f}%")
        c3.metric("Positions",     len(rows))

        st.markdown("---")

        # Holding cards
        cols = st.columns(min(len(rows), 3))
        for i, r in enumerate(rows):
            with cols[i % 3]:
                pnl_h  = _pnl_html(r["P&L %"])
                arr    = "▲" if r["24h %"] > 0 else "▼"
                cls24  = "pos" if r["24h %"] > 0 else "neg"
                amt_dec = 2 if r["Amount"]>=1 else 4 if r["Amount"]>=0.01 else 6
                decimals = 2 if r["Price $"]>=1 else 4 if r["Price $"]>=0.01 else 6 if r["Price $"]>=0.0001 else 8
                st.markdown(
                    f'<div class="glass">'
                    f'<b style="font-size:1.15rem;color:#f0f8ff">{r["Asset"]}</b>'
                    f'<span style="float:right;color:#4a7a9b;font-size:0.85rem">{r["Amount"]:.{amt_dec}f}</span><br>'
                    f'<span style="font-size:1.35rem;color:#38bdf8;font-weight:700">${r["Price $"]:.{decimals}f}</span><br>'
                    f'<span style="color:#94b8d8">Value:</span> <b>${r["Value $"]:.2f}</b>'
                    f'&nbsp;&nbsp;<span style="color:#94b8d8">P&L:</span> {pnl_h}<br>'
                    f'<span class="{cls24}">{arr} {r["24h %"]:+.2f}% (24h)</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        st.markdown("---")
        col_pie, col_tbl = st.columns([1,1])

        with col_pie:
            st.markdown("### Allocation")
            fig_pie = px.pie(
                port_df, values="Value $", names="Asset",
                color_discrete_sequence=AERO_PALETTE, hole=0.42,
            )
            fig_pie.update_traces(textinfo="label+percent", textfont_color="#f0f8ff",
                                  marker=dict(line=dict(color="rgba(0,0,0,0.3)",width=1)))
            fig_pie.update_layout(height=320, showlegend=False, **PLOT_LAYOUT)
            st.plotly_chart(fig_pie, use_container_width=True)

        with col_tbl:
            st.markdown("### Holdings Table")
            disp = port_df.drop(columns=["_coin_id"])
            def _color(val):
                if isinstance(val, float):
                    return "color:#6ee7b7" if val>0 else "color:#fca5a5" if val<0 else ""
                return ""
            st.dataframe(
                disp.style.applymap(_color, subset=["P&L %","24h %","7d %"])
                    .format({"Amount":".4f","Price $":"${:.4f}","Value $":"${:.2f}",
                             "Entry $":"${:.4f}","P&L %":"{:+.2f}%",
                             "24h %":"{:+.2f}%","7d %":"{:+.2f}%"}, na_rep="—"),
                use_container_width=True, hide_index=True,
            )

        # Price history
        if not hist.empty:
            st.markdown("---")
            st.markdown("### Price History")
            avail = hist["coin"].unique().tolist()
            sel = st.multiselect("Coins", avail, default=avail[:4])

            entry_map: dict[str,float] = {}
            for h in holdings:
                ep = h.get("entry_price_usd")
                if ep:
                    entry_map[h["asset"]] = ep

            if sel:
                fig_h = go.Figure()
                for i, coin in enumerate(sel):
                    cd = hist[hist["coin"]==coin].sort_values("timestamp")
                    fig_h.add_trace(go.Scatter(x=cd["timestamp"],y=cd["price_usd"],name=coin,mode="lines",
                        line=dict(color=AERO_PALETTE[i%len(AERO_PALETTE)],width=2),
                        fill="tozeroy",fillcolor=f"rgba({','.join(str(int(AERO_PALETTE[i%len(AERO_PALETTE)].lstrip('#')[j*2:j*2+2],16)) for j in range(3))},0.06)"))
                    if len(sel)==1 and coin in entry_map:
                        fig_h.add_hline(y=entry_map[coin],line_color="#fbbf24",line_dash="dash",
                            annotation_text=f"Entry ${entry_map[coin]:.4f}",
                            annotation_position="bottom right")
                fig_h.update_layout(height=380, hovermode="x unified",
                                    xaxis_title="", yaxis_title="Price (USD)", **PLOT_LAYOUT)
                st.plotly_chart(fig_h, use_container_width=True)


# ═════════════════════════════════════════════════════════════════════════
# PAGE: SCANNER PICKS
# ═════════════════════════════════════════════════════════════════════════
elif page == "🎯 Scanner Picks":
    st.markdown("# 🎯 Scanner Picks")

    df = load_recommendations()
    if df.empty:
        st.warning("No data yet. Click **▶ Run Scan** in the sidebar.")
    else:
        scanner_df = df[df.get("type",pd.Series(dtype=str)).isin(["SCANNER",""])] if "type" in df.columns else df.copy()

        n_open  = len(scanner_df[scanner_df["status"]=="OPEN"])
        n_win   = len(scanner_df[scanner_df["status"]=="WIN"])
        n_loss  = len(scanner_df[scanner_df["status"]=="LOSS"])
        closed  = n_win + n_loss
        win_rate= (n_win/closed*100) if closed else 0
        closed_df = scanner_df[scanner_df["status"].isin(["WIN","LOSS"])]
        avg_pnl   = closed_df["pnl_pct"].mean() if not closed_df.empty else 0.0

        # Expectancy
        if closed >= 10:
            wins_pnl   = closed_df[closed_df["status"]=="WIN"]["pnl_pct"].dropna()
            losses_pnl = closed_df[closed_df["status"]=="LOSS"]["pnl_pct"].dropna()
            wr_frac    = n_win/closed if closed else 0
            lr_frac    = n_loss/closed if closed else 0
            avg_win    = wins_pnl.mean() if not wins_pnl.empty else 0
            avg_loss   = abs(losses_pnl.mean()) if not losses_pnl.empty else 0
            expectancy = (wr_frac * avg_win/100) - (lr_frac * avg_loss/100)
            exp_str    = f"{expectancy:+.3f}R"
            exp_label  = f"(50+ needed for validity)" if closed < 50 else "✅ statistically valid"
        else:
            exp_str    = "N/A"
            exp_label  = f"need 10+ closed trades ({closed} so far)"

        c1,c2,c3,c4,c5 = st.columns(5)
        c1.metric("Total Picks",  len(scanner_df))
        c2.metric("Open",         n_open)
        c3.metric("Wins",         n_win)
        c4.metric("Losses",       n_loss)
        c5.metric("Win Rate",     f"{win_rate:.0f}%", f"avg {avg_pnl:+.1f}%")

        st.markdown(
            f'<div class="glass" style="margin-top:8px">'
            f'<b>📐 Expectancy:</b> <span style="color:#38bdf8;font-size:1.1rem">{exp_str}</span>'
            f'&nbsp;&nbsp;<small>{exp_label}</small>'
            f'</div>',
            unsafe_allow_html=True,
        )

        st.markdown("---")

        # Open positions
        open_df = scanner_df[scanner_df["status"]=="OPEN"]
        if not open_df.empty:
            st.markdown("### 🔓 Open Positions")
            live_prices = load_live_prices()
            for _, row in open_df.iterrows():
                entry   = row.get("entry_price")
                sl      = row.get("stop_loss")
                tp      = row.get("take_profit")
                cid     = row.get("coin_id","")
                live_p  = live_prices.get(cid)
                curr_usd = live_p.price_usd if live_p else (row.get("current_price") or None)
                curr_eur = live_p.price_eur if live_p else (row.get("price_eur") or None)

                try:
                    pnl = ((float(curr_usd)-float(entry))/float(entry)*100) if curr_usd and entry else None
                except Exception:
                    pnl = row.get("pnl_pct")

                bar_html = ""
                if entry and sl and tp and curr_usd:
                    try:
                        rng = float(tp)-float(sl)
                        pos = max(0,min(100,(float(curr_usd)-float(sl))/rng*100))
                        bar_col = "green" if pos>50 else "red"
                        bar_html = (
                            f'{_aero_bar(pos, bar_col)}'
                            f'<small>SL ${float(sl):.6f} → TP ${float(tp):.6f}</small>'
                        )
                    except Exception:
                        pass

                price_dec = 4 if curr_usd and float(curr_usd)<1 else 2
                reasoning = str(row.get("reasoning",""))
                web_tag = ""
                if "WEB RESEARCH: CONFIRM" in reasoning or "web_research_verdict" in reasoning:
                    web_tag = '&nbsp;<span style="color:#6ee7b7;font-size:0.72rem">✅ Web confirmed</span>'
                elif "WEB RESEARCH: CHANGE" in reasoning or "switched from" in reasoning:
                    web_tag = '&nbsp;<span style="color:#fbbf24;font-size:0.72rem">⚠️ Web changed pick</span>'

                st.markdown(
                    f'<div class="glass">'
                    f'<b style="font-size:1.05rem;color:#38bdf8">{row.get("coin","?")}</b>'
                    f'<span style="float:right">{_status_badge("OPEN")}'
                    f'{"&nbsp;<span style=\"color:#f87171;font-size:0.72rem\">🔴 live</span>" if live_p else ""}</span><br>'
                    f'Entry: <b style="color:#f0f8ff">${float(entry):.{price_dec}f}</b>'
                    f'&nbsp;→&nbsp;Now: <b style="color:#f0f8ff">${float(curr_usd) if curr_usd else 0:.{price_dec}f}</b>'
                    f'&nbsp;&nbsp;P&L: {_pnl_html(pnl)}{web_tag}'
                    f'{bar_html}'
                    f'<br><small style="color:#4a7a9b">{str(row.get("date",""))[:16]}'
                    f'&nbsp;|&nbsp;{row.get("timeframe","")}</small>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        st.markdown("---")

        # P&L distribution
        if closed > 0:
            st.markdown("### 📊 P&L Distribution (closed trades)")
            fig_dist = go.Figure()
            w_data = closed_df[closed_df["status"]=="WIN"]["pnl_pct"].dropna()
            l_data = closed_df[closed_df["status"]=="LOSS"]["pnl_pct"].dropna()
            if not w_data.empty:
                fig_dist.add_trace(go.Histogram(x=w_data, name="Win",  marker_color="#34d399", nbinsx=15, opacity=0.8))
            if not l_data.empty:
                fig_dist.add_trace(go.Histogram(x=l_data, name="Loss", marker_color="#f87171", nbinsx=15, opacity=0.8))
            fig_dist.update_layout(barmode="overlay", height=280,
                                   xaxis_title="P&L %", **PLOT_LAYOUT)
            st.plotly_chart(fig_dist, use_container_width=True)

        # Per-coin charts
        if not open_df.empty:
            hist_sc = load_price_history()
            if not hist_sc.empty:
                st.markdown("---")
                st.markdown("### 📈 Open Position Charts")
                open_coins = open_df["coin"].dropna().unique().tolist()
                tabs = st.tabs(open_coins)
                for i, coin in enumerate(open_coins):
                    with tabs[i]:
                        cd       = hist_sc[hist_sc["coin"]==coin].sort_values("timestamp")
                        coin_row = open_df[open_df["coin"]==coin].iloc[0]
                        entry_v  = coin_row.get("entry_price")
                        sl_v     = coin_row.get("stop_loss")
                        tp_v     = coin_row.get("take_profit")
                        if cd.empty:
                            st.info(f"No price history for {coin} yet.")
                            continue
                        fig_c = go.Figure()
                        fig_c.add_trace(go.Scatter(x=cd["timestamp"],y=cd["price_usd"],name=coin,mode="lines",
                            line=dict(color="#38bdf8",width=2.2),
                            fill="tozeroy",fillcolor="rgba(56,189,248,0.07)"))
                        for val,col,label,pos in [
                            (entry_v,"#fbbf24","Entry","bottom right"),
                            (sl_v,"#f87171","SL","top right"),
                            (tp_v,"#34d399","TP","top right"),
                        ]:
                            if val and pd.notna(val):
                                fig_c.add_hline(y=float(val),line_color=col,line_dash="dash",
                                    annotation_text=f"{label} ${float(val):.6f}",
                                    annotation_position=pos,
                                    annotation_font_color=col)
                        fig_c.update_layout(height=320,hovermode="x unified",
                                            xaxis_title="",yaxis_title="USD",**PLOT_LAYOUT)
                        st.plotly_chart(fig_c, use_container_width=True)


# ═════════════════════════════════════════════════════════════════════════
# PAGE: TECHNICAL ANALYSIS
# ═════════════════════════════════════════════════════════════════════════
elif page == "📐 Technical Analysis":
    st.markdown("# 📐 Technical Analysis")
    st.caption("Watchlist coins — RSI(14), MACD, Bollinger Bands, support/resistance")

    with st.spinner("Loading TA (fetches 30d OHLCV per coin)…"):
        ta_data = load_technical_analysis()

    if not ta_data:
        st.warning("No TA data. Click ▶ Run Scan or wait for auto-refresh.")
    else:
        prices = load_live_prices()
        for coin_id, ta in ta_data.items():
            symbol = config.WATCHLIST_SYMBOLS.get(coin_id, coin_id.upper())
            p      = prices.get(coin_id)
            price_str = _price_fmt(p.price_eur) if p else "—"
            conf_pct  = f"{ta.confidence:.0%}" if ta.confidence else "—"

            # RSI color
            rsi = ta.rsi_14
            rsi_color = "#f87171" if (rsi and rsi < 30) else "#34d399" if (rsi and rsi > 70) else "#fbbf24" if (rsi and rsi < 40) else "#7dd3fc"
            rsi_str   = f"{rsi:.1f}" if rsi else "N/A"

            # MACD
            macd_color = "#34d399" if ta.macd_signal=="BULLISH" else "#f87171" if ta.macd_signal=="BEARISH" else "#94b8d8"

            supp = ", ".join(f"${s:.4f}" for s in (ta.support_levels or [])[:3]) or "—"
            res  = ", ".join(f"${r:.4f}" for r in (ta.resistance_levels or [])[:3]) or "—"

            st.markdown(
                f'<div class="glass">'
                f'<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">'
                f'<span style="font-size:1.25rem;font-weight:800;color:#f0f8ff">{symbol}</span>'
                f'<span style="font-size:1.1rem;color:#38bdf8;font-weight:700">{price_str}</span>'
                f'{_trend_badge(ta.trend)}'
                f'<span style="color:#4a7a9b;font-size:0.78rem">conf: {conf_pct}</span>'
                f'</div>'
                f'<div style="display:flex;gap:24px;flex-wrap:wrap">'
                f'<div><span style="color:#7dd3fc;font-size:0.72rem">RSI(14)</span><br>'
                f'<span style="color:{rsi_color};font-size:1.4rem;font-weight:700">{rsi_str}</span></div>'
                f'<div><span style="color:#7dd3fc;font-size:0.72rem">MACD</span><br>'
                f'<span style="color:{macd_color};font-weight:700">{ta.macd_signal or "N/A"}</span></div>'
                f'<div><span style="color:#7dd3fc;font-size:0.72rem">Bollinger</span><br>'
                f'<span style="color:#d0e8ff;font-weight:600">{ta.bollinger_position or "N/A"}</span></div>'
                f'</div>'
                f'<div style="margin-top:8px;font-size:0.82rem">'
                f'<span style="color:#34d399">▼ Support:</span> <span style="color:#d0e8ff">{supp}</span>'
                f'&nbsp;&nbsp;'
                f'<span style="color:#f87171">▲ Resist:</span> <span style="color:#d0e8ff">{res}</span>'
                f'</div>'
                f'<div style="margin-top:6px;color:#94b8d8;font-size:0.80rem;font-style:italic">💡 {ta.key_observation or "—"}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )


# ═════════════════════════════════════════════════════════════════════════
# PAGE: CHARTS
# ═════════════════════════════════════════════════════════════════════════
elif page == "📈 Charts":
    st.markdown("# 📈 Price Charts")

    hist   = load_price_history()
    prices = load_live_prices()
    df_rec = load_recommendations()

    if hist.empty:
        st.info("No price history yet — run ▶ Run Scan to start logging data.")
    else:
        avail = sorted(hist["coin"].dropna().unique().tolist())
        col_sel, col_cur, col_days = st.columns([3,1,1])
        selected  = col_sel.multiselect("Select coins", avail, default=avail[:6])
        currency  = col_cur.radio("Currency", ["USD","EUR"], horizontal=True)
        day_window= col_days.selectbox("Window", ["All","7d","3d","1d"])

        pc  = "price_usd" if currency=="USD" else "price_eur"
        sym = "$" if currency=="USD" else "€"

        if not selected:
            st.info("Select at least one coin.")
        else:
            plot_hist = hist[hist["coin"].isin(selected)].copy()
            if day_window != "All":
                days_map = {"7d":7,"3d":3,"1d":1}
                cutoff = pd.Timestamp.now()-pd.Timedelta(days=days_map[day_window])
                plot_hist["timestamp"] = pd.to_datetime(plot_hist["timestamp"],errors="coerce")
                plot_hist = plot_hist[plot_hist["timestamp"] >= cutoff]

            # Relative performance
            st.markdown("### Relative Performance (indexed to 100)")
            fig_norm = go.Figure()
            for i, coin in enumerate(selected):
                cd   = plot_hist[plot_hist["coin"]==coin].sort_values("timestamp")
                vals = cd[pc].dropna()
                if vals.empty or vals.iloc[0]==0:
                    continue
                normed = vals/vals.iloc[0]*100
                fig_norm.add_trace(go.Scatter(x=cd["timestamp"].iloc[:len(normed)],y=normed,
                    name=coin,mode="lines",line=dict(color=AERO_PALETTE[i%len(AERO_PALETTE)],width=2)))
            fig_norm.add_hline(y=100,line_color="rgba(255,255,255,0.15)",line_dash="dash")
            fig_norm.update_layout(height=360,hovermode="x unified",**PLOT_LAYOUT)
            st.plotly_chart(fig_norm, use_container_width=True)

            st.markdown("---")
            st.markdown("### Individual Charts")
            tabs = st.tabs(selected)
            open_rec = df_rec[df_rec["status"]=="OPEN"] if not df_rec.empty else pd.DataFrame()

            for i, coin in enumerate(selected):
                with tabs[i]:
                    cd = plot_hist[plot_hist["coin"]==coin].sort_values("timestamp")
                    if cd.empty:
                        st.info(f"No data for {coin} in window.")
                        continue
                    live_p = next((p for cid,p in prices.items() if p.symbol==coin),None)
                    fig_c  = go.Figure()
                    color  = AERO_PALETTE[i%len(AERO_PALETTE)]
                    fig_c.add_trace(go.Scatter(x=cd["timestamp"],y=cd[pc],name=coin,mode="lines",
                        line=dict(color=color,width=2.2),fill="tozeroy",
                        fillcolor=f"rgba({','.join(str(int(color.lstrip('#')[j*2:j*2+2],16)) for j in range(3))},0.07)"))
                    if live_p:
                        lv = live_p.price_usd if currency=="USD" else live_p.price_eur
                        fig_c.add_trace(go.Scatter(x=[cd["timestamp"].iloc[-1]],y=[lv],
                            mode="markers",marker=dict(color="#fbbf24",size=10),name=f"Live {sym}{lv:.4f}"))

                    rec_row = open_rec[open_rec["coin"]==coin] if not open_rec.empty else pd.DataFrame()
                    if not rec_row.empty:
                        row = rec_row.iloc[0]
                        ev = row.get("entry_price"); sv = row.get("stop_loss"); tv = row.get("take_profit")
                        if currency=="EUR" and live_p and live_p.price_usd:
                            rate = live_p.price_eur/live_p.price_usd
                            ev = float(ev)*rate if pd.notna(ev) else None
                            sv = float(sv)*rate if pd.notna(sv) else None
                            tv = float(tv)*rate if pd.notna(tv) else None
                        for v,col,label in [(ev,"#fbbf24","Entry"),(sv,"#f87171","SL"),(tv,"#34d399","TP")]:
                            if v and pd.notna(v):
                                fig_c.add_hline(y=float(v),line_color=col,line_dash="dash",
                                    annotation_text=f"{label} {sym}{float(v):.4f}",
                                    annotation_position="bottom right",annotation_font_color=col)

                    if len(cd) >= 2:
                        fv = cd[pc].dropna().iloc[0]; lv_last = cd[pc].dropna().iloc[-1]
                        if fv:
                            chg = (lv_last-fv)/fv*100
                            chg_col = "#34d399" if chg>=0 else "#f87171"
                            st.markdown(
                                f'<p style="color:{chg_col};font-weight:700;font-size:1.05rem">'
                                f'{coin} &nbsp;{sym}{lv_last:.6f} &nbsp;{chg:+.2f}% ({day_window})</p>',
                                unsafe_allow_html=True,
                            )

                    fig_c.update_layout(height=340,hovermode="x unified",**PLOT_LAYOUT)
                    st.plotly_chart(fig_c, use_container_width=True)

                    if live_p:
                        ci1,ci2,ci3,ci4 = st.columns(4)
                        lval = live_p.price_usd if currency=="USD" else live_p.price_eur
                        ci1.metric("Price",f"{sym}{lval:.6f}")
                        ci2.metric("24h",f"{live_p.change_24h:+.2f}%")
                        ci3.metric("7d", f"{live_p.change_7d:+.2f}%")
                        ci4.metric("MCap",f"${live_p.market_cap/1e6:.0f}M")


# ═════════════════════════════════════════════════════════════════════════
# PAGE: ALL SIGNALS
# ═════════════════════════════════════════════════════════════════════════
elif page == "📰 All Signals":
    st.markdown("# 📰 All Signals & Log")

    df = load_recommendations()
    if df.empty:
        st.info("No signals logged yet.")
    else:
        col1,col2,col3 = st.columns(3)
        types    = df["type"].dropna().unique().tolist() if "type" in df.columns else []
        statuses = df["status"].dropna().unique().tolist() if "status" in df.columns else []
        tf = col1.multiselect("Type",   types,    default=types)
        sf = col2.multiselect("Status", statuses, default=statuses)
        cf = col3.text_input("Coin filter","")

        filtered = df.copy()
        if tf and "type" in filtered.columns:
            filtered = filtered[filtered["type"].isin(tf)]
        if sf and "status" in filtered.columns:
            filtered = filtered[filtered["status"].isin(sf)]
        if cf:
            filtered = filtered[filtered["coin"].str.upper().str.contains(cf.upper(),na=False)]

        st.caption(f"{len(filtered)} of {len(df)} rows")
        st.dataframe(filtered.sort_values("date",ascending=False),
                     use_container_width=True, hide_index=True)


# ═════════════════════════════════════════════════════════════════════════
# PAGE: POLYMARKET
# ═════════════════════════════════════════════════════════════════════════
elif page == "🔮 Polymarket":
    st.markdown("# 🔮 Polymarket Prediction Markets")

    poly = load_polymarket()
    if not poly:
        st.warning("No Polymarket data. Check connection.")
    else:
        poly_sorted = sorted(poly, key=lambda m: m.get("volume_usd",0), reverse=True)
        high_conf   = [m for m in poly_sorted if m.get("probability",0.5)>=0.70 or m.get("probability",0.5)<=0.30]
        st.metric("High-Conviction Markets (≥70% / ≤30%)", len(high_conf))
        st.markdown("---")

        cols = st.columns(2)
        for i, m in enumerate(poly_sorted):
            q = m.get("question","")
            if not q:
                continue
            prob     = m.get("probability")
            vol      = m.get("volume_usd",0)
            prob_pct = f"{prob*100:.0f}%" if prob is not None else "?"
            vol_str  = f"${vol/1000:.0f}k" if vol>=1000 else f"${vol:.0f}"
            bar_pct  = int((prob or 0.5)*100)
            bar_col  = "green" if (prob or 0)>=0.60 else "red" if (prob or 0.5)<0.40 else "yellow"
            icon     = "🟢" if (prob or 0)>=0.65 else "🔴" if (prob or 0.5)<0.35 else "🟡"

            with cols[i%2]:
                st.markdown(
                    f'<div class="glass">'
                    f'<span style="font-size:0.88rem;color:#d0e8ff">{q}</span><br>'
                    f'{_aero_bar(bar_pct, bar_col)}'
                    f'{icon} <span style="font-size:1.3rem;font-weight:800;color:#f0f8ff">{prob_pct}</span>'
                    f'&nbsp;&nbsp;<small>Vol: {vol_str}</small>'
                    f'</div>',
                    unsafe_allow_html=True,
                )


# ═════════════════════════════════════════════════════════════════════════
# PAGE: POLYMARKET ADVISOR
# ═════════════════════════════════════════════════════════════════════════
elif page == "🔮 Polymarket Advisor":
    st.markdown("# 🔮 Polymarket Advisor")
    st.caption("LLM-analysed prediction markets — EDGE DETECTED = odds mispriced by >20%")

    poly_live = load_polymarket()
    picks_df  = load_polymarket_picks()

    # Live markets
    if poly_live:
        st.markdown("### 🌐 Live Markets (top by volume)")
        poly_sorted = sorted(poly_live, key=lambda m: m.get("volume_usd",0), reverse=True)
        cols = st.columns(2)
        for i, m in enumerate(poly_sorted):
            q = m.get("question","")
            if not q:
                continue
            prob     = m.get("probability")
            vol      = m.get("volume_usd",0)
            prob_str = f"{prob*100:.0f}%" if prob is not None else "?"
            vol_str  = f"${vol/1000:.0f}k" if vol>=1000 else f"${vol:.0f}"
            bar_pct  = int((prob or 0.5)*100)
            bar_col  = "green" if (prob or 0)>=0.60 else "red" if (prob or 0.5)<0.40 else "yellow"
            icon     = "🟢" if (prob or 0)>=0.65 else "🔴" if (prob or 0.5)<0.35 else "🟡"
            with cols[i%2]:
                st.markdown(
                    f'<div class="glass">'
                    f'<span style="font-size:0.85rem;color:#d0e8ff">{q}</span><br>'
                    f'{_aero_bar(bar_pct, bar_col)}'
                    f'{icon} <b style="color:#f0f8ff;font-size:1.2rem">{prob_str}</b>'
                    f'&nbsp;&nbsp;<small>Vol: {vol_str}</small>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # LLM Picks history
    st.markdown("---")
    st.markdown("### 🤖 LLM Pick History")
    if picks_df.empty:
        st.info("No Polymarket picks yet — run ▶ Run Scan to generate LLM analysis.")
    else:
        # KPIs
        total    = len(picks_df)
        n_yes    = (picks_df["llm_verdict"]=="YES").sum() if "llm_verdict" in picks_df.columns else 0
        n_edge   = (picks_df["is_opportunity"]=="yes").sum() if "is_opportunity" in picks_df.columns else 0
        n_web    = picks_df["web_sentiment"].notna().sum() if "web_sentiment" in picks_df.columns else 0
        c1,c2,c3,c4 = st.columns(4)
        c1.metric("Total Picks",        total)
        c2.metric("YES Verdicts",       int(n_yes))
        c3.metric("🎯 Edges Detected",  int(n_edge))
        c4.metric("🌐 Web Researched",  int(n_web))

        # Edge opportunities highlighted
        if "is_opportunity" in picks_df.columns:
            edge_df = picks_df[picks_df["is_opportunity"]=="yes"].copy()
            if not edge_df.empty:
                st.markdown("#### 🎯 EDGE DETECTED — Mispriced Markets")
                for _, row in edge_df.iterrows():
                    verdict = row.get("llm_verdict","?")
                    conf    = row.get("llm_confidence_pct","")
                    edge    = row.get("llm_edge_pct","")
                    odds    = row.get("current_odds_pct","")
                    conf_str = f"{conf:.0f}%" if isinstance(conf, float) else str(conf)
                    edge_str = f"+{edge:.0f}%" if isinstance(edge, float) else str(edge)
                    odds_str = f"{odds:.0f}%" if isinstance(odds, float) else str(odds)
                    v_icon   = "✅" if verdict=="YES" else "❌"
                    web_s = row.get("web_sentiment","")
                    web_tag = ""
                    if web_s == "BULLISH":
                        web_tag = '&nbsp;<span style="color:#6ee7b7;font-size:0.72rem">🌐 Web: BULLISH</span>'
                    elif web_s == "BEARISH":
                        web_tag = '&nbsp;<span style="color:#fca5a5;font-size:0.72rem">🌐 Web: BEARISH</span>'
                    elif web_s:
                        web_tag = f'&nbsp;<span style="color:#94b8d8;font-size:0.72rem">🌐 Web: {web_s}</span>'

                    st.markdown(
                        f'<div class="glass" style="border-color:rgba(251,191,36,0.4)">'
                        f'<span style="color:#fbbf24;font-weight:700">🎯 EDGE DETECTED</span>'
                        f'<span style="float:right;color:#94b8d8;font-size:0.75rem">{str(row.get("date",""))[:16]}</span><br>'
                        f'<span style="color:#f0f8ff;font-size:0.88rem">{row.get("market","")[:90]}</span><br>'
                        f'Market odds: <b style="color:#f87171">{odds_str}</b>'
                        f'&nbsp;→&nbsp;LLM estimate: <b style="color:#34d399">{edge_str} edge</b><br>'
                        f'{v_icon} Verdict: <b style="color:#7dd3fc">{verdict}</b>'
                        f'&nbsp;Confidence: <b>{conf_str}</b>{web_tag}'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

        st.markdown("---")
        with st.expander("📋 Full Pick Log"):
            st.dataframe(picks_df.sort_values("date", ascending=False),
                        use_container_width=True, hide_index=True)


# ═════════════════════════════════════════════════════════════════════════
# PAGE: STOCK SCANNER
# ═════════════════════════════════════════════════════════════════════════
elif page == "📊 Stock Scanner":
    st.markdown("# 📊 Stock Scanner")
    st.caption("S&P 500 top 30 · NASDAQ tech · Crypto-related stocks — ranked by TA opportunity score")

    stock_df = load_stock_recommendations()

    if stock_df.empty:
        st.info("No stock data yet. Click ▶ Run Scan (or run `python run.py --stocks`) to populate.")
    else:
        scanner_stocks = stock_df.copy()
        n_open  = (scanner_stocks["status"]=="OPEN").sum()
        n_win   = (scanner_stocks["status"]=="WIN").sum()
        n_loss  = (scanner_stocks["status"]=="LOSS").sum()
        closed  = n_win + n_loss
        wr      = f"{n_win/closed*100:.0f}%" if closed else "—"
        closed_s = scanner_stocks[scanner_stocks["status"].isin(["WIN","LOSS"])]
        avg_pnl  = closed_s["pnl_pct"].mean() if not closed_s.empty else 0.0

        c1,c2,c3,c4,c5 = st.columns(5)
        c1.metric("Total Picks", len(scanner_stocks))
        c2.metric("Open",        int(n_open))
        c3.metric("Wins",        int(n_win))
        c4.metric("Losses",      int(n_loss))
        c5.metric("Win Rate",    wr, f"avg {avg_pnl:+.1f}%")

        st.markdown("---")

        # Open positions
        open_s = scanner_stocks[scanner_stocks["status"]=="OPEN"]
        if not open_s.empty:
            st.markdown("### 🔓 Open Stock Positions")
            cols = st.columns(min(len(open_s), 3))
            for i, (_, row) in enumerate(open_s.iterrows()):
                with cols[i % 3]:
                    entry  = row.get("entry_price")
                    curr   = row.get("current_price")
                    pnl    = row.get("pnl_pct")
                    sl     = row.get("stop_loss")
                    tp     = row.get("take_profit")
                    pnl_h  = _pnl_html(pnl)
                    bar_html = ""
                    if entry and sl and tp and curr and pd.notna(curr):
                        try:
                            rng = float(tp)-float(sl)
                            pos = max(0,min(100,(float(curr)-float(sl))/rng*100))
                            bar_html = _aero_bar(pos,"green" if pos>50 else "red")
                        except Exception:
                            pass
                    reasoning_s = str(row.get("reasoning",""))
                    web_tag_s = ""
                    if "CONFIRM" in reasoning_s:
                        web_tag_s = '&nbsp;<span style="color:#6ee7b7;font-size:0.7rem">✅ Web confirmed</span>'
                    elif "CHANGE" in reasoning_s or "web_research" in reasoning_s:
                        web_tag_s = '&nbsp;<span style="color:#fbbf24;font-size:0.7rem">⚠️ Web flagged</span>'

                    st.markdown(
                        f'<div class="glass">'
                        f'<b style="font-size:1.1rem;color:#f0f8ff">{row.get("symbol","?")}</b>'
                        f'<span style="float:right">{_status_badge("OPEN")}</span><br>'
                        f'<span style="font-size:0.78rem;color:#7dd3fc">{row.get("name","")[:30]}</span><br>'
                        f'Entry: <b style="color:#fbbf24">${float(entry):,.2f}</b>'
                        f'&nbsp;Now: <b style="color:#f0f8ff">${float(curr):,.2f}</b><br>'
                        f'P&L: {pnl_h}{web_tag_s}'
                        f'{"&nbsp;&nbsp;P/E: <span style=\"color:#94b8d8\">" + str(round(row["pe_ratio"],1)) + "</span>" if pd.notna(row.get("pe_ratio")) else ""}'
                        f'{bar_html}'
                        f'<small style="color:#4a7a9b">SL ${float(sl):,.2f} → TP ${float(tp):,.2f}</small>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

        st.markdown("---")

        # P&L histogram
        if closed > 0:
            st.markdown("### 📊 P&L Distribution")
            fig_s = go.Figure()
            w_s = closed_s[closed_s["status"]=="WIN"]["pnl_pct"].dropna()
            l_s = closed_s[closed_s["status"]=="LOSS"]["pnl_pct"].dropna()
            if not w_s.empty:
                fig_s.add_trace(go.Histogram(x=w_s, name="Win",  marker_color="#34d399", nbinsx=12, opacity=0.8))
            if not l_s.empty:
                fig_s.add_trace(go.Histogram(x=l_s, name="Loss", marker_color="#f87171", nbinsx=12, opacity=0.8))
            fig_s.update_layout(barmode="overlay", height=260, xaxis_title="P&L %", **PLOT_LAYOUT)
            st.plotly_chart(fig_s, use_container_width=True)

        st.markdown("---")
        with st.expander("📋 Full Stock Pick Log"):
            st.dataframe(scanner_stocks.sort_values("date", ascending=False),
                        use_container_width=True, hide_index=True)


# ═════════════════════════════════════════════════════════════════════════
# PAGE: LLM COSTS
# ═════════════════════════════════════════════════════════════════════════
elif page == "💸 LLM Costs":
    st.markdown("# 💸 LLM Usage & Budget")

    llm_df = load_llm_stats()
    today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if llm_df.empty:
        st.info("No LLM calls logged yet. Run ▶ Run Scan to start.")
    else:
        today_df    = llm_df[llm_df["date"]==today]
        today_calls = int(today_df["calls"].sum()) if not today_df.empty else 0
        today_cost  = float(today_df["cost_usd"].sum()) if not today_df.empty else 0.0
        today_tok   = int(
            (today_df.get("tokens_in",pd.Series([0])).fillna(0)
            +today_df.get("tokens_out",pd.Series([0])).fillna(0)).sum()
        ) if not today_df.empty else 0

        c1,c2,c3 = st.columns(3)
        c1.metric("Today's Calls",  today_calls)
        c2.metric("Today's Tokens", f"{today_tok:,}")
        c3.metric("Today's Cost",   f"${today_cost:.4f}")

        st.markdown("---")
        st.markdown("### Budget Status")
        for model, limits in config.DAILY_BUDGET_LIMITS.items():
            model_today = today_df[today_df["model"]==model] if not today_df.empty else pd.DataFrame()
            calls_used  = int(model_today["calls"].sum()) if not model_today.empty else 0
            max_calls   = limits.get("max_calls",0)
            if max_calls:
                pct      = min(calls_used/max_calls, 1.0)
                bar_col  = "green" if pct<0.5 else "yellow" if pct<0.8 else "red"
                st.markdown(
                    f'<div class="glass">'
                    f'<b style="color:#38bdf8">{model}</b>: '
                    f'<span style="color:#f0f8ff">{calls_used}/{max_calls} calls</span>'
                    f'{_aero_bar(pct*100, bar_col)}'
                    f'<small>{pct*100:.0f}% of daily limit</small>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        st.markdown("---")
        st.markdown("### Calls per Day")
        daily_agg = llm_df.groupby("date")["calls"].sum().reset_index()
        fig_calls = go.Figure(go.Bar(x=daily_agg["date"],y=daily_agg["calls"],
            marker=dict(color=AERO_PALETTE[0],
                        line=dict(color="rgba(255,255,255,0.08)",width=1))))
        fig_calls.update_layout(height=280, xaxis_title="", yaxis_title="Calls", **PLOT_LAYOUT)
        st.plotly_chart(fig_calls, use_container_width=True)

        with st.expander("Full LLM Log"):
            st.dataframe(llm_df, use_container_width=True)

# ── Global scan output (bottom of every page except Run Scanner + Overview) ──
if st.session_state["scan_output"] and page not in ("🚀 Run Scanner", "📊 Overview"):
    st.markdown("---")
    ts_str = st.session_state["last_scan_ts"].strftime("%H:%M:%S") if st.session_state["last_scan_ts"] else ""
    with st.expander(f"📟 Last Scan Output  [{ts_str}]"):
        st.markdown(f'<div class="terminal">{st.session_state["scan_output"]}</div>', unsafe_allow_html=True)
