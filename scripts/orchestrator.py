#!/usr/bin/env python3
"""
Single-process prediction + validation + live watching.
Runs for ~59 minutes. No race conditions.
"""
import json
import os
import random
import time
import urllib.request
from datetime import datetime, timezone
import httpx

# ═══════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════

S1_URL = "https://asdzxcvzxcvx-s1.hf.space"
OKX_API = "https://www.okx.com"
PREDICTIONS_DIR = "data/predictions"
ACCURACY_FILE = "data/accuracy.json"

COINS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "MATIC/USDT", "DOT/USDT"
]

TIMEFRAMES = ["15m", "1h", "4h"]

RUN_DURATION = 3540        # 59 minutes (safe margin)
POLL_INTERVAL = 5           # seconds between live price checks
VERIFY_CHECK_EVERY = 60     # seconds between verification scans


# ═══════════════════════════════════════
# FILE HELPERS
# ═══════════════════════════════════════

def prediction_path(symbol: str, timeframe: str, ts: int) -> str:
    """Build path: data/predictions/YYYY-MM/DD/SYMBOL_TF_TS.json"""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    symbol_clean = symbol.replace("/", "")
    filename = f"{symbol_clean}_{timeframe}_{ts}.json"
    return os.path.join(
        PREDICTIONS_DIR,
        dt.strftime("%Y-%m"),
        dt.strftime("%d"),
        filename
    )


def find_prediction_files():
    """Yield all prediction JSON file paths."""
    if not os.path.exists(PREDICTIONS_DIR):
        return
    for root, dirs, files in os.walk(PREDICTIONS_DIR):
        for f in sorted(files):
            if f.endswith(".json"):
                yield os.path.join(root, f)


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ═══════════════════════════════════════
# PRICE FETCHER
# ═══════════════════════════════════════

def fetch_prices(symbols: set) -> dict:
    """Fetch current prices from OKX for a set of symbols."""
    prices = {}
    for symbol in symbols:
        try:
            inst_id = symbol.replace("/", "-") + "-SWAP"
            url = f"{OKX_API}/api/v5/market/ticker?instId={inst_id}"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                prices[symbol] = float(data["data"][0]["last"])
        except Exception:
            pass
    return prices


# ═══════════════════════════════════════
# PHASE 1: MAKE PREDICTIONS
# ═══════════════════════════════════════

async def make_predictions():
    """Make 2-4 predictions for random coins/timeframes."""
    num = random.randint(2, 4)
    selected = [
        (random.choice(COINS), random.choice(TIMEFRAMES))
        for _ in range(num)
    ]

    print(f"\n{'='*60}")
    print(f"📡 PHASE 1: Making {len(selected)} predictions")
    print(f"{'='*60}")

    made = 0
    async with httpx.AsyncClient(timeout=90.0) as client:
        for symbol, timeframe in selected:
            try:
                resp = await client.get(
                    f"{S1_URL}/api/predict",
                    params={"symbol": symbol, "timeframe": timeframe}
                )

                if resp.status_code != 200:
                    print(f"  ❌ {symbol}/{timeframe} — HTTP {resp.status_code}")
                    continue

                result = resp.json()
                now_ts = int(time.time())
                tf_minutes = {"15m": 15, "1h": 60, "4h": 240}[timeframe]
                due_ts = now_ts + (tf_minutes * 60)

                prediction = {
                    "id": f"{symbol.replace('/', '')}-{timeframe}-{now_ts}",
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "predicted_at": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
                    "predicted_at_ts": now_ts,
                    "due_at_ts": due_ts,
                    "due_at": datetime.fromtimestamp(due_ts, tz=timezone.utc).isoformat(),
                    "current_price": result["currentPrice"],
                    "predicted_mean": result["predictedMean"],
                    "bull_case": result["bullCase"],
                    "bear_case": result["bearCase"],
                    "median_case": result["medianCase"],
                    "confidence": result["confidenceScore"],
                    "directional_signal": result.get("directionalSignal", "neutral"),
                    "directional_bias": result.get("directionalBias", 0),
                    "verified": False,
                    "verification": None,
                    "tp_reached_early": False,
                    "sl_reached_early": False,
                    "tp_reached_at": None,
                    "sl_reached_at": None,
                }

                filepath = prediction_path(symbol, timeframe, now_ts)
                save_json(filepath, prediction)
                print(f"  ✅ {symbol}/{timeframe} — due in {tf_minutes} min → {filepath}")
                made += 1

            except Exception as e:
                print(f"  ❌ {symbol}/{timeframe} — {e}")

    print(f"  💾 {made} predictions saved")


# ═══════════════════════════════════════
# PHASE 2: VERIFY DUE PREDICTIONS
# ═══════════════════════════════════════

def verify_due_predictions():
    """Check all prediction files. Verify any that are past their due time."""
    now_ts = time.time()
    verified = 0

    for filepath in list(find_prediction_files()):
        pred = load_json(filepath)

        # Skip already verified
        if pred.get("verified"):
            continue

        # Check if due (within window: -5 min to +60 min)
        due_ts = pred.get("due_at_ts", 0)
        time_diff = now_ts - due_ts

        if time_diff < -300 or time_diff > 3600:
            continue

        # Fetch current price
        prices = fetch_prices({pred["symbol"]})
        actual = prices.get(pred["symbol"])

        if actual is None:
            print(f"  ⚠️ Could not fetch price for {pred['symbol']}")
            continue

        # Add verification
        pred["verification"] = {
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "actual_price": actual,
            "deviation_pct": round(
                (actual - pred["predicted_mean"]) / pred["current_price"] * 100, 2
            ),
            "in_range": pred["bear_case"] <= actual <= pred["bull_case"],
        }
        pred["verified"] = True

        # Check TP/SL
        if actual >= pred["bull_case"]:
            pred["tp_reached_early"] = True
            pred["tp_reached_at"] = datetime.now(timezone.utc).isoformat()
            print(f"  🎯 TP HIT: {pred['symbol']} ({pred['timeframe']}) — {actual} ≥ {pred['bull_case']}")

        if actual <= pred["bear_case"]:
            pred["sl_reached_early"] = True
            pred["sl_reached_at"] = datetime.now(timezone.utc).isoformat()
            print(f"  🛑 SL HIT: {pred['symbol']} ({pred['timeframe']}) — {actual} ≤ {pred['bear_case']}")

        save_json(filepath, pred)
        verified += 1

        status = "✅"
        if pred["verification"]["in_range"]:
            status = "🎯"
        print(f"  {status} Verified: {pred['symbol']} ({pred['timeframe']}) "
              f"— predicted {pred['predicted_mean']:.2f}, actual {actual:.2f}, "
              f"deviation {pred['verification']['deviation_pct']}%")

    return verified


# ═══════════════════════════════════════
# PHASE 3: LIVE WATCH
# ═══════════════════════════════════════

def watch_cycle():
    """Check all active (unverified, non-expired) predictions for early TP/SL."""
    now_ts = time.time()
    active_symbols = set()
    active_preds = []

    for filepath in find_prediction_files():
        pred = load_json(filepath)

        # Active = not verified, not already hit TP/SL, not expired
        if (not pred.get("verified") and
            not pred.get("tp_reached_early") and
            not pred.get("sl_reached_early") and
            (pred.get("due_at_ts", 0) - now_ts) > -300):
            active_symbols.add(pred["symbol"])
            active_preds.append((filepath, pred))

    if not active_preds:
        return 0

    prices = fetch_prices(active_symbols)
    hits = 0

    for filepath, pred in active_preds:
        price = prices.get(pred["symbol"])
        if price is None:
            continue

        updated = False

        if price >= pred["bull_case"] and not pred.get("tp_reached_early"):
            pred["tp_reached_early"] = True
            pred["tp_reached_at"] = datetime.now(timezone.utc).isoformat()
            updated = True
            hits += 1
            print(f"  🎯 LIVE TP: {pred['symbol']} ({pred['timeframe']}) at {price}")

        if price <= pred["bear_case"] and not pred.get("sl_reached_early"):
            pred["sl_reached_early"] = True
            pred["sl_reached_at"] = datetime.now(timezone.utc).isoformat()
            updated = True
            hits += 1
            print(f"  🛑 LIVE SL: {pred['symbol']} ({pred['timeframe']}) at {price}")

        if updated:
            save_json(filepath, pred)

    return len(active_preds)


# ═══════════════════════════════════════
# ACCURACY COMPUTATION
# ═══════════════════════════════════════

def compute_and_save_accuracy():
    """Read all verified predictions and compute accuracy stats."""
    verified = []

    for filepath in find_prediction_files():
        pred = load_json(filepath)
        if pred.get("verified") and pred.get("verification"):
            verified.append(pred)

    total = len(verified)

    if total == 0:
        print("  📭 No verified predictions yet")
        return None

    correct_dir = 0
    wrong_dir = 0
    in_range = 0
    hit_tp = sum(1 for p in verified if p.get("tp_reached_early"))
    hit_sl = sum(1 for p in verified if p.get("sl_reached_early"))

    for p in verified:
        v = p["verification"]
        actual = v["actual_price"]
        pred_dir = p["directional_signal"]

        # Directional accuracy
        if pred_dir == "bullish" and actual > p["current_price"]:
            correct_dir += 1
        elif pred_dir == "bearish" and actual < p["current_price"]:
            correct_dir += 1
        elif pred_dir == "neutral":
            pass  # neutral doesn't count
        else:
            wrong_dir += 1

        # Range accuracy
        if p["bear_case"] <= actual <= p["bull_case"]:
            in_range += 1

    acc = {
        "total_verified": total,
        "correct_direction": correct_dir,
        "wrong_direction": wrong_dir,
        "hit_tp_early": hit_tp,
        "hit_sl_early": hit_sl,
        "in_range": in_range,
        "accuracy_pct": round(in_range / total * 100, 1) if total > 0 else 0,
        "directional_accuracy_pct": round(
            correct_dir / (correct_dir + wrong_dir) * 100, 1
        ) if (correct_dir + wrong_dir) > 0 else 0,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

    save_json(ACCURACY_FILE, acc)

    print(f"\n{'='*60}")
    print(f"📊 ACCURACY STATS")
    print(f"{'='*60}")
    print(f"  Total verified:          {acc['total_verified']}")
    print(f"  In range (bull/bear):    {acc['in_range']} ({acc['accuracy_pct']}%)")
    print(f"  Directional correct:     {acc['correct_direction']} ({acc['directional_accuracy_pct']}%)")
    print(f"  Directional wrong:       {acc['wrong_direction']}")
    print(f"  TP hit early:            {acc['hit_tp_early']}")
    print(f"  SL hit early:            {acc['hit_sl_early']}")
    print(f"  Last updated:            {acc['last_updated']}")

    return acc


# ═══════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════

async def main():
    start_time = datetime.now(timezone.utc)
    print("=" * 60)
    print(f"🚀 ORCHESTRATOR STARTED")
    print(f"   Time:     {start_time.isoformat()}")
    print(f"   Duration: {RUN_DURATION}s (~{RUN_DURATION//60} min)")
    print(f"   Poll:     every {POLL_INTERVAL}s")
    print(f"   Verify:   every {VERIFY_CHECK_EVERY}s")
    print("=" * 60)

    # Phase 1: Make predictions
    await make_predictions()

    # Phase 2: Main loop — verify + watch
    loop_start = time.time()
    last_verify = loop_start
    cycle_count = 0

    while time.time() - loop_start < RUN_DURATION:
        cycle_count += 1

        # Periodic verification
        if time.time() - last_verify >= VERIFY_CHECK_EVERY:
            elapsed = int(time.time() - loop_start)
            print(f"\n🔍 Verification scan at T+{elapsed}s")
            v = verify_due_predictions()
            if v > 0:
                compute_and_save_accuracy()
            last_verify = time.time()

        # Live watch every cycle
        active = watch_cycle()

        # Progress indicator (every 2 minutes)
        if cycle_count % 24 == 0:
            elapsed = int(time.time() - loop_start)
            print(f"  ⏱️ T+{elapsed}s — {active} active predictions")

        time.sleep(POLL_INTERVAL)

    # Phase 3: Final accuracy save
    print(f"\n⏰ Time's up after {int(time.time() - loop_start)}s")
    print("Running final verification...")
    verify_due_predictions()
    compute_and_save_accuracy()

    print(f"\n✅ Orchestrator finished at {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
