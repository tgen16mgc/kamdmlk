import logging
import threading
import urllib.request
import urllib.parse
import json

logger = logging.getLogger("telegram")

BOT_TOKEN = "8470419869:AAGkrUWs7yBoMjhN3nZ7gyVPaMkkrLPzPtc"
CHAT_ID = "1666338978"

EMOJIS = {
    "BUY":  "🟢",
    "TP":   "✅",
    "SL":   "🛑",
    "TIME": "⏱️",
    "WIN":  "🏆",
    "LOSS": "💀",
}


def _send(text: str):
    """Fire-and-forget POST to Telegram in a background thread."""
    def _post():
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            payload = json.dumps({
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            }).encode()
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception as e:
            logger.debug(f"Telegram send failed: {e}")

    threading.Thread(target=_post, daemon=True).start()


def notify_trade(action: str, details: str, pnl: float | None = None, balance: float | None = None):
    emoji = EMOJIS.get(action, "📌")
    lines = [f"{emoji} <b>{action}</b>  |  {details}"]
    if pnl is not None:
        sign = "+" if pnl >= 0 else ""
        lines.append(f"PnL: <b>{sign}${pnl:.3f}</b>")
    if balance is not None:
        lines.append(f"Balance: <b>${balance:.2f}</b>")
    _send("\n".join(lines))


def notify_daily_report(
    wins: int,
    losses: int,
    session_pnl: float,
    balance: float,
    starting_balance: float,
    btc_price: float | None = None,
):
    total = wins + losses
    winrate = (wins / total * 100) if total > 0 else 0
    pnl_sign = "+" if session_pnl >= 0 else ""
    pnl_emoji = "📈" if session_pnl >= 0 else "📉"

    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"📊 <b>Daily Report</b>  —  {now}",
        "",
        f"W / L:       <b>{wins} / {losses}</b>  ({winrate:.0f}% win rate)",
        f"Session PnL: <b>{pnl_sign}${session_pnl:.2f}</b> {pnl_emoji}",
        f"Balance:     <b>${balance:.2f}</b>",
        f"Start bal:   <b>${starting_balance:.2f}</b>",
    ]
    if btc_price is not None:
        lines.append(f"BTC price:   <b>${btc_price:,.0f}</b>")

    _send("\n".join(lines))
