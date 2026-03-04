"""
Telegram notification helper.
"""

import requests

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from utils.logger import get_logger

log = get_logger(__name__)


def send_telegram(message: str) -> bool:
    """Send a message via the Telegram Bot API. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.debug("Telegram not configured — skipping notification.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True
        log.warning("Telegram API returned %s: %s", resp.status_code, resp.text)
    except requests.RequestException as exc:
        log.error("Telegram send failed: %s", exc)
    return False


def notify_trade_entry(
    instrument: str,
    direction: str,
    price: float,
    qty: int,
    sl: float,
    target: float,
    strategy: str,
    risk_amount: float,
) -> None:
    emoji = "\U0001f7e2" if direction == "BUY" else "\U0001f534"
    msg = (
        f"{emoji} *{direction} {instrument}* @ \u20b9{price:,.2f}\n"
        f"Qty: {qty} | SL: \u20b9{sl:,.2f} | Target: \u20b9{target:,.2f}\n"
        f"Strategy: {strategy} | Risk: \u20b9{risk_amount:,.0f}"
    )
    send_telegram(msg)


def notify_trade_exit(
    instrument: str,
    exit_price: float,
    pnl: float,
    reason: str,
    duration_min: int,
) -> None:
    emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
    tag = "TARGET HIT" if reason == "TARGET_HIT" else reason.replace("_", " ")
    msg = (
        f"{emoji} *{tag}* \u2014 {instrument}\n"
        f"Exit: \u20b9{exit_price:,.2f} | P&L: \u20b9{pnl:+,.0f}\n"
        f"Duration: {duration_min} min"
    )
    send_telegram(msg)


def notify_daily_summary(
    date_str: str,
    total: int,
    wins: int,
    losses: int,
    gross_pnl: float,
    net_pnl: float,
    max_dd: float,
) -> None:
    win_rate = (wins / total * 100) if total else 0
    msg = (
        f"\U0001f4ca *Daily Summary* \u2014 {date_str}\n"
        f"Trades: {total} | Win: {wins} | Loss: {losses}\n"
        f"Gross P&L: \u20b9{gross_pnl:+,.0f} | Net: \u20b9{net_pnl:+,.0f}\n"
        f"Win Rate: {win_rate:.0f}% | Max DD: \u20b9{max_dd:,.0f}"
    )
    send_telegram(msg)


def notify_risk_breach(reason: str) -> None:
    send_telegram(f"\u26a0\ufe0f *RISK BREACH*: {reason}")


def notify_bot_status(status: str) -> None:
    send_telegram(f"\U0001f916 Bot *{status}*")


def notify_order_error(instrument: str, error: str) -> None:
    send_telegram(f"\u274c *Order Error* — {instrument}: {error}")
