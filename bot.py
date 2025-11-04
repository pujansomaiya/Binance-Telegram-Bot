#!/usr/bin/env python3
"""
bot.py — Multi-agent majority-rule trading bot (Binance connected)

SAFE DEFAULTS:
- LIVE_ORDERS = False  (simulation mode: uses real market prices but does NOT send orders)
- USE_BINANCE_TESTNET = True recommended for testing placing real orders

Features:
- Connects to Binance via ccxt (public price fetch, and optional order execution)
- 9 mocked AI agents (stubs). Each returns buy/sell/hold + confidence.
- Majority decision (simple count). Tie -> skip.
- Spot trading (market orders) only. No futures/margin in this script.
- Persist trades & engine weights to SQLite.
- Configurable via environment variables.

Required packages:
pip install ccxt pandas

Environment variables:
- BINANCE_API_KEY (optional for price only; required for real orders)
- BINANCE_API_SECRET
- USE_BINANCE_TESTNET ("true" / "false") - if true, bot will point CCXT to Binance testnet
- LIVE_ORDERS ("true" / "false") - if true, bot will place real orders (testnet or mainnet depending on above)
- SYMBOLS (comma separated like "BTC/USDT,ETH/USDT")
- TRADE_USD (per trade notional)
- CYCLE_SECONDS (loop wait)
"""

import os, time, json, math, random, sqlite3
from datetime import datetime
import ccxt
import pandas as pd

# ------------------ CONFIG (via env vars) ------------------
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")        # put keys in Replit secrets / Render env
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
USE_BINANCE_TESTNET = os.getenv("USE_BINANCE_TESTNET", "true").lower() in ("1","true","yes")
LIVE_ORDERS = os.getenv("LIVE_ORDERS", "false").lower() in ("1","true","yes")
SYMBOLS = os.getenv("SYMBOLS", "BTC/USDT,ETH/USDT,BNB/USDT").split(",")
TRADE_USD = float(os.getenv("TRADE_USD", "100.0"))
CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "30"))
TP_PCT = float(os.getenv("TP_PCT", "2.0"))
SL_PCT = float(os.getenv("SL_PCT", "3.0"))
DB_PATH = os.getenv("DB_PATH", "trades.db")
ENGINE_NAMES = ["GPT-5","Grok","Gemini","Claude","DeepSeek","Perplexity","Mistral","Llama-3","Falcon"]
WEIGHT_LR = float(os.getenv("WEIGHT_LR", "0.04"))

# ------------------ Safety check ------------------
if LIVE_ORDERS and not BINANCE_API_KEY:
    raise SystemExit("LIVE_ORDERS requested but BINANCE_API_KEY not provided. Aborting for safety.")

# ------------------ Persistence ------------------
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS trades (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      time TEXT,
      symbol TEXT,
      side TEXT,
      entry REAL,
      exit REAL,
      pnl_usd REAL,
      pnl_pct REAL,
      result TEXT,
      details TEXT
    );
    CREATE TABLE IF NOT EXISTS engine_weights (
      engine TEXT PRIMARY KEY,
      weight REAL
    );
    CREATE TABLE IF NOT EXISTS engine_history (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      engine TEXT,
      time TEXT,
      trade_id INTEGER,
      pnl REAL,
      contrib REAL
    );
    """)
    # init weights
    for e in ENGINE_NAMES:
        cur.execute("INSERT OR IGNORE INTO engine_weights (engine, weight) VALUES (?, ?)", (e, 1.0))
    conn.commit()
    return conn

conn = init_db()

def get_weights():
    cur = conn.cursor()
    cur.execute("SELECT engine, weight FROM engine_weights")
    return {r[0]: r[1] for r in cur.fetchall()}

def set_weight(engine, w):
    cur = conn.cursor()
    cur.execute("UPDATE engine_weights SET weight = ? WHERE engine = ?", (float(w), engine))
    conn.commit()

# ------------------ Exchange setup (ccxt) ------------------
def make_exchange():
    params = {'enableRateLimit': True}
    exchange = ccxt.binance(params)
    if BINANCE_API_KEY:
        exchange.apiKey = BINANCE_API_KEY
        exchange.secret = BINANCE_API_SECRET
    # switch to sandbox/testnet for Binance (supported by ccxt)
    if USE_BINANCE_TESTNET:
        try:
            exchange.set_sandbox_mode(True)
            print("INFO: Binance sandbox/testnet mode enabled.")
        except Exception as e:
            print("WARN: Could not enable sandbox mode via ccxt:", e)
    return exchange

exchange = make_exchange()

def fetch_price(symbol):
    try:
        t = exchange.fetch_ticker(symbol)
        return float(t['last'])
    except Exception as e:
        print("Price fetch error", symbol, e)
        return None

# ------------------ Agent stubs (replace later with real API calls) ------------------
def stub_agent(engine_name, symbol):
    # lightweight stochastic stub — replace with actual LLM API call
    r = random.random()
    if r < 0.42:
        vote = "buy"
    elif r < 0.84:
        vote = "sell"
    else:
        vote = "hold"
    confidence = round(random.uniform(0.45, 0.95), 2)
    return {"engine": engine_name, "vote": vote, "confidence": confidence, "rationale": f"{engine_name} {vote}"}

def collect_signals(symbol):
    responses = []
    for e in ENGINE_NAMES:
        try:
            resp = stub_agent(e, symbol)
            responses.append(resp)
        except Exception as ex:
            print("Agent error", e, ex)
    return responses

def majority_decision(responses):
    counts = {"buy":0,"sell":0,"hold":0}
    for r in responses:
        counts[r["vote"]] += 1
    max_votes = max(counts.values())
    winners = [k for k,v in counts.items() if v==max_votes]
    if len(winners) != 1:
        return {"decision":"hold", "counts":counts}
    return {"decision":winners[0], "counts":counts}

# ------------------ Position simulation / order wrapper ------------------
open_positions = {}  # trade_id -> position dict
trade_seq = 0

def simulate_open(symbol, side, price):
    global trade_seq
    trade_seq += 1
    entry = float(price)
    notional = TRADE_USD
    # quantity in base units
    qty = (notional) / entry if entry>0 else 0
    pos = {
        "trade_id": trade_seq,
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "qty": qty,
        "tp": entry * (1 + TP_PCT/100.0) if side=="long" else entry * (1 - TP_PCT/100.0),
        "sl": entry * (1 - SL_PCT/100.0) if side=="long" else entry * (1 + SL_PCT/100.0),
        "open_time": datetime.utcnow().isoformat()
    }
    open_positions[trade_seq] = pos
    print(f"SIM OPEN {pos}")
    return pos

def simulate_close(trade_id, exit_price, reason="tp/sl/timeout"):
    pos = open_positions.get(trade_id)
    if not pos:
        return None
    entry = pos["entry"]
    notional = TRADE_USD
    if pos["side"]=="long":
        pnl_pct = (exit_price - entry) / entry * 100.0
    else:
        pnl_pct = (entry - exit_price) / entry * 100.0
    pnl_usd = notional * (pnl_pct/100.0)
    trade = {
        "trade_id": trade_id,
        "time": datetime.utcnow().isoformat(),
        "symbol": pos["symbol"],
        "side": pos["side"],
        "entry": entry,
        "exit": exit_price,
        "pnl_usd": round(pnl_usd,2),
        "pnl_pct": round(pnl_pct,4),
        "result": "win" if pnl_usd>0 else "loss",
        "details": pos
    }
    # persist
    cur = conn.cursor()
    cur.execute("INSERT INTO trades (time,symbol,side,entry,exit,pnl_usd,pnl_pct,result,details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trade["time"], trade["symbol"], trade["side"], trade["entry"], trade["exit"], trade["pnl_usd"], trade["pnl_pct"], trade["result"], json.dumps(trade["details"])))
    conn.commit()
    # update weights
    update_weights_from_trade(trade, pos.get("responses", []))
    del open_positions[trade_id]
    print("SIM CLOSE", trade)
    return trade

# ------------------ Order placement (if LIVE_ORDERS True) ------------------
def place_market_order(symbol, side, amount_usd):
    """
    Places a market order for `amount_usd` worth of base asset (spot).
    Returns executed price or None.
    """
    try:
        price = fetch_price(symbol)
        if price is None:
            return None
        base_amount = amount_usd / price
        # For spot market order via ccxt:
        # symbol format like "BTC/USDT"
        side_ccxt = "buy" if side=="long" else "sell"
        # convert to actual market order
        # WARNING: minimum order sizes exist; adjust rounding as needed
        order = exchange.create_market_order(symbol, side_ccxt, base_amount)
        exec_price = float(order['average']) if order.get('average') else float(order['price']) if order.get('price') else price
        return exec_price
    except Exception as e:
        print("Order error:", e)
        return None

# ------------------ Weights / Learning ------------------
def update_weights_from_trade(trade, responses):
    pnl = float(trade["pnl_usd"])
    if pnl == 0:
        return
    cur_weights = get_weights()
    for r in responses:
        name = r["engine"]
        vote = r["vote"]
        conf = float(r.get("confidence", 0.5))
        profitable_side = "buy" if pnl>0 else "sell"
        align = 1.0 if vote==profitable_side else -1.0
        score = conf * align * math.sqrt(abs(pnl) + 1.0)
        old_w = float(cur_weights.get(name, 1.0))
        new_w = old_w * math.exp(WEIGHT_LR * score)
        new_w = max(0.1, min(new_w, 20.0))
        set_weight(name, new_w)
        cur = conn.cursor()
        cur.execute("INSERT INTO engine_history (engine, time, trade_id, pnl, contrib) VALUES (?, ?, ?, ?, ?)",
                    (name, datetime.utcnow().isoformat(), trade["trade_id"], pnl, score))
        conn.commit()

# ------------------ Main loop ------------------
def main_loop():
    print("Starting bot. LIVE_ORDERS =", LIVE_ORDERS, "TESTNET=", USE_BINANCE_TESTNET)
    weights = get_weights()
    cycle = 0
    while True:
        cycle += 1
        # 1) close positions if TP/SL hit (use live price)
        for tid, pos in list(open_positions.items()):
            price_now = fetch_price(pos["symbol"])
            if price_now is None:
                continue
            if (pos["side"]=="long" and price_now >= pos["tp"]) or (pos["side"]=="short" and price_now <= pos["tp"]):
                simulate_close(tid, pos["tp"])
            elif (pos["side"]=="long" and price_now <= pos["sl"]) or (pos["side"]=="short" and price_now >= pos["sl"]):
                simulate_close(tid, pos["sl"])

        # 2) for each symbol, collect signals and possibly open new position
        for symbol in SYMBOLS:
            responses = collect_signals(symbol)
            # attach responses to pos later to track attribution
            agg = majority_decision(responses)
            decision = agg["decision"]
            if decision in ("buy","sell"):
                side = "long" if decision=="buy" else "short"
                price = fetch_price(symbol)
                if price is None:
                    continue
                # If LIVE_ORDERS True, place real market order (on testnet/mainnet per config)
                if LIVE_ORDERS:
                    exec_price = place_market_order(symbol, side, TRADE_USD)
                    if exec_price is None:
                        print("Order failed, skipping")
                        continue
                    pos = simulate_open(symbol, side, exec_price)
                else:
                    pos = simulate_open(symbol, side, price)
                # attach responses for feedback later
                pos["responses"] = responses
                # small delay to avoid rate limits
                time.sleep(0.5)
            # else: hold => no action

        # 3) housekeeping / wait
        if cycle % 10 == 0:
            print(f"{datetime.utcnow().isoformat()} cycle {cycle} open_positions={len(open_positions)} db_trades={len(pd.read_sql('SELECT * FROM trades', conn))}")
        time.sleep(CYCLE_SECONDS)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        print("Stopping bot by user.")
        conn.close()
