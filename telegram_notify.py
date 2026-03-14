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
