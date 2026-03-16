import os
from dotenv import load_dotenv

load_dotenv()

# ── Wallet ────────────────────────────────────────────────────────────────────
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
RELAYER_API_KEY = os.getenv("RELAYER_API_KEY")
RELAYER_API_KEY_ADDRESS = os.getenv("RELAYER_API_KEY_ADDRESS")
SIGNER_ADDRESS = os.getenv("SIGNER_ADDRESS")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS")
SIGNATURE_TYPE = 1  # POLY_PROXY (Magic Link / email login)
CHAIN_ID = 137  # Polygon

# ── API Endpoints ─────────────────────────────────────────────────────────────
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
RTDS_WS = "wss://ws-live-data.polymarket.com"
MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ── Entry Conditions ──────────────────────────────────────────────────────────
ENTRY_WINDOW_MAX = 160  # seconds remaining (2 min 40 sec) - enter no earlier
ENTRY_WINDOW_MIN = 50   # seconds remaining (0:50) - enter no later
ENTRY_PRICE_MIN = 0.60  # minimum acceptable token price for entry (medium momentum)
ENTRY_PRICE_MAX = 0.70  # maximum acceptable token price for entry (medium momentum)
BTC_MOMENTUM_MIN = 30.0  # minimum BTC price change ($) from candle open (low tier)
BTC_MOMENTUM_MED = 45.0  # medium momentum threshold (original minimum)
BTC_MOMENTUM_HIGH = 65.0  # high momentum threshold to shift entry price range
ENTRY_PRICE_MIN_LOW_MOM = 0.52   # min entry price when momentum < BTC_MOMENTUM_MED
ENTRY_PRICE_MAX_LOW_MOM = 0.60   # max entry price when momentum < BTC_MOMENTUM_MED
ENTRY_PRICE_MIN_HIGH_MOM = 0.65  # min entry price when momentum >= BTC_MOMENTUM_HIGH
ENTRY_PRICE_MAX_HIGH_MOM = 0.75  # max entry price when momentum >= BTC_MOMENTUM_HIGH
MOMENTUM_VELOCITY_WINDOW = 10  # seconds to check momentum velocity
MAX_SPREAD = 0.05  # max bid-ask spread to accept entry

# ── Exit Conditions ───────────────────────────────────────────────────────────
TAKE_PROFIT = 0.92   # sell when token price >= this
STOP_LOSS = 0.39     # sell when token price <= this
TIME_STOP_SECONDS = 30  # soft exit — skipped if momentum still supports the direction
BREAKEVEN_TIME_STOP_SECONDS = 35  # exit early if price <= entry with this many seconds left
HARD_TIME_STOP_SECONDS = 20  # absolute hard exit — sell no matter what below this

# ── Bet Sizing ────────────────────────────────────────────────────────────────
BET_SIZE = 2   # default bet in USDC
ALL_IN = False   # if True, use full USDC balance instead of BET_SIZE

# ── Risk Management ──────────────────────────────────────────────────────────
CONSECUTIVE_LOSS_LIMIT = 3  # pause after N consecutive losses
COOLDOWN_ROUNDS = 1  # rounds to skip when loss limit is hit
SESSION_STOP_LOSS_PCT = 0.60  # stop trading after losing this % of starting balance

# ── Order Retry / Rejection Handling ─────────────────────────────────────────
BUY_REJECT_COOLDOWN = 1    # seconds to wait after a buy rejection before retrying
SELL_MAX_RETRIES = 5         # after this many FOK failures, switch to FAK
SELL_FAK_ATTEMPTS = 3        # FAK attempts before giving up and letting market resolve

# ── Proxy (optional) ─────────────────────────────────────────────────────────
# Set PROXY_URL in environment to route all traffic through a proxy.
# Format: http://user:pass@host:port  or  http://host:port
PROXY_URL = os.getenv("PROXY_URL", "")  # empty = no proxy

# ── Timing ────────────────────────────────────────────────────────────────────
STRATEGY_LOOP_INTERVAL = 0.1  # seconds between strategy ticks
MARKET_POLL_INTERVAL = 3.0    # seconds between Gamma API polls
HEARTBEAT_INTERVAL = 5.0      # seconds between CLOB heartbeats
WS_PING_INTERVAL = 10.0       # seconds between WebSocket pings
