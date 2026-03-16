# BTC 5-Min Momentum Bot

Automated trading bot for Polymarket's 5-minute BTC up/down prediction markets.

## Strategy

Buys the momentum direction (Up or Down token) when BTC shows strong directional movement within a strict time window, then exits via take-profit, stop-loss, or time-stop rules.

**Entry**: 3:00 to 0:45 remaining | 3-tier momentum system: BTC $30+ move → token $0.52-$0.60, BTC $45+ → $0.60-$0.70, BTC $65+ → $0.65-$0.75 | momentum still accelerating

**Exit**: TP at $0.88 | SL at $0.30 | Time stop at 30s remaining

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file with your credentials (see `.env` for the template).

## Run

```bash
python main.py
```

## Configuration

All tunable parameters are in `config.py`: entry/exit thresholds, bet size, risk limits, timing intervals.

Set `ALL_IN = True` in `config.py` to bet your full USDC balance instead of the default $1.
