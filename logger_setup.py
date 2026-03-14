import logging
import os
import sys
from datetime import datetime


RESET = "\033[0m"
COLORS = {
    "DEBUG": "\033[36m",     # cyan
    "INFO": "\033[32m",      # green
    "WARNING": "\033[33m",   # yellow
    "ERROR": "\033[31m",     # red
    "CRITICAL": "\033[41m",  # red background
}

TRADE_COLORS = {
    "BUY": "\033[1;32m",    # bold green
    "SELL": "\033[1;31m",   # bold red
    "TP": "\033[1;33m",     # bold yellow
    "SL": "\033[1;31m",     # bold red
    "TIME": "\033[1;35m",   # bold magenta
    "WIN": "\033[1;32m",    # bold green
    "LOSS": "\033[1;31m",   # bold red
}


class ColoredFormatter(logging.Formatter):
    def format(self, record):
        color = COLORS.get(record.levelname, "")
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        msg = record.getMessage()
        return f"{color}[{ts}] [{record.levelname:<7}] {record.name}: {msg}{RESET}"


def setup_logging(level=logging.INFO):
    os.makedirs("logs", exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(ColoredFormatter())
    root.addHandler(console)

    file_handler = logging.FileHandler(
        f"logs/bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root.addHandler(file_handler)

    trade_handler = logging.FileHandler("logs/trades.log")
    trade_handler.setLevel(logging.INFO)
    trade_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(message)s")
    )
    trade_logger = logging.getLogger("trades")
    trade_logger.addHandler(trade_handler)
    trade_logger.propagate = True

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("ClobClient").setLevel(logging.WARNING)


def log_trade(action: str, details: str, pnl: float | None = None, balance: float | None = None):
    color = TRADE_COLORS.get(action, "")
    logger = logging.getLogger("trades")
    logger.info(f"{color}{action:<5}{RESET} | {details}")

    from telegram_notify import notify_trade
    notify_trade(action, details, pnl=pnl, balance=balance)
