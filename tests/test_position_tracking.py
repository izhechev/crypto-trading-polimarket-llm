import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.agents import groq_analyst
from src.utils import logger, price_alerts


HEADERS = [
    "date", "type", "coin", "coin_id", "position_id", "entry_price",
    "stop_loss", "take_profit", "status", "exit_price", "close_date",
    "pnl_pct", "current_price", "price_eur", "timeframe", "fear_greed",
    "reasoning", "recommended_order", "groq_rank", "qualifier", "key_signal",
]


def write_recommendations(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_recommendations(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class PositionTrackingTests(unittest.TestCase):
    def test_log_whale_ride_persists_stop_loss_and_take_profit(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "recommendations.csv"
            with patch.object(logger, "LOG_PATH", csv_path):
                logger.log_whale_ride({
                    "symbol": "SNT",
                    "coin_id": "snt-status",
                    "entry": 0.011,
                    "stop_loss": 0.00935,
                    "take_profit": 0.0165,
                    "crash_reason": "test",
                }, fear_greed_value=47)

            row = read_recommendations(csv_path)[0]
            self.assertEqual(row["stop_loss"], "0.00935")
            self.assertEqual(row["take_profit"], "0.0165")

    def test_price_alerts_refreshes_legacy_whale_rows_without_sl_tp(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "recommendations.csv"
            state_path = Path(tmp) / "alert_state.json"
            write_recommendations(csv_path, [{
                "date": "2026-05-10 23:14 UTC",
                "type": "WHALE_RIDE",
                "coin": "SNT",
                "coin_id": "snt-status",
                "entry_price": "1.0",
                "stop_loss": "",
                "take_profit": "",
                "status": "OPEN",
                "current_price": "1.0",
                "recommended_order": "LONG",
            }])

            with patch.object(price_alerts, "_CSV_PATH", csv_path), \
                 patch.object(price_alerts, "_ALERT_STATE_PATH", state_path), \
                 patch.object(price_alerts, "_fetch_prices_usd", return_value={"snt-status": 1.05}):
                price_alerts.check_price_alerts()

            row = read_recommendations(csv_path)[0]
            self.assertEqual(row["current_price"], "1.05")
            self.assertEqual(row["pnl_pct"], "5.0")
            self.assertEqual(row["status"], "OPEN")

    def test_logger_refreshes_coinpaprika_id_via_coingecko_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "recommendations.csv"
            write_recommendations(csv_path, [{
                "date": "2026-05-10 23:14 UTC",
                "type": "WHALE_RIDE",
                "coin": "SNT",
                "coin_id": "snt-status",
                "entry_price": "1.0",
                "status": "OPEN",
                "current_price": "1.0",
                "recommended_order": "LONG",
            }])

            with patch.object(logger, "LOG_PATH", csv_path), \
                 patch("src.connectors.coingecko.fetch_prices", return_value=[]), \
                 patch("src.connectors.coingecko.fetch_simple_usd", return_value={"status": 1.05}), \
                 patch("src.connectors.coinpaprika.resolve_cg_id", return_value="status"), \
                 patch("src.connectors.binance.fetch_binance_ticker", return_value=None, create=True), \
                 patch("src.connectors.kraken.fetch_kraken_ticker", return_value=None, create=True):
                logger.update_open_positions()

            row = read_recommendations(csv_path)[0]
            self.assertEqual(row["current_price"], "1.05")
            self.assertEqual(row["pnl_pct"], "5.0")

    def test_groq_prefilter_keeps_borderline_bullish_rsi(self):
        rows = [{
            "symbol": "UNI",
            "name": "Uniswap",
            "macd": "BULLISH",
            "trend": "BULLISH",
            "bb_pos": "MIDDLE",
            "supply_risk": "NONE",
            "rsi": 73,
            "vol_mcap": 0.16,
            "change_24h": 4.9,
            "change_7d": 24,
            "score": 9,
            "recommended_order": "LONG",
            "reasons": [],
            "price": 1.0,
        }]

        candidates, hard_removed, _ = groq_analyst._prefilter_groq_candidates(rows, already_open=set())

        self.assertEqual([c["symbol"] for c in candidates], ["UNI"])
        self.assertEqual(hard_removed, [])


if __name__ == "__main__":
    unittest.main()
