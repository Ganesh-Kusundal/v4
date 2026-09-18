"""Empirical test of Dhan's actual rate limits for 90-day 1m historical data."""
import time
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add brokers to path
sys.path.insert(0, str(Path(__file__).parent / "brokers" / "src"))
sys.path.insert(0, str(Path(__file__).parent / "domain" / "src"))
sys.path.insert(0, str(Path(__file__).parent / "trading" / "src"))

from tradex_domain.instruments import Equity
from tradex_domain.value_objects import InstrumentId
from tradex_domain.timeframe import Timeframe


def load_dhan():
    """Load Dhan broker with real credentials."""
    import os
    from dotenv import load_dotenv
    load_dotenv(".env.local")
    
    from tradex_trading.runtime.live import build_broker_from_env
    
    broker = build_broker_from_env("dhan")
    broker.connect()
    return broker


def test_single_90day_call():
    """Test 1: Single call for 90 days of 1m data."""
    print("\n" + "="*60)
    print("TEST 1: Single call for 90 days of 1m data")
    print("="*60)
    
    dhan = load_dhan()
    instrument = Equity.of("NSE", "RELIANCE")
    
    start = datetime(2025, 1, 1)
    end = datetime(2025, 3, 31)  # 90 days
    
    print(f"Symbol: {instrument.instrument_id}")
    print(f"Range: {start.date()} to {end.date()} ({(end - start).days} days)")
    print(f"Timeframe: 1m")
    
    t0 = time.time()
    try:
        series = dhan.history(instrument, Timeframe.M1, start, end)
        elapsed = time.time() - t0
        print(f"✓ SUCCESS: Got {len(series.candles)} candles in {elapsed:.2f}s")
        print(f"  First candle: {series.candles[0].timestamp}")
        print(f"  Last candle: {series.candles[-1].timestamp}")
        return True, elapsed, len(series.candles)
    except Exception as e:
        elapsed = time.time() - t0
        print(f"✗ FAILED after {elapsed:.2f}s: {e}")
        return False, elapsed, 0


def test_5_parallel_requests():
    """Test 2: 5 parallel requests for 90-day 1m data."""
    print("\n" + "="*60)
    print("TEST 2: 5 parallel requests for 90-day 1m data")
    print("="*60)
    
    import concurrent.futures
    
    dhan = load_dhan()
    symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]
    
    start = datetime(2025, 1, 1)
    end = datetime(2025, 3, 31)  # 90 days
    
    print(f"Symbols: {symbols}")
    print(f"Range: {start.date()} to {end.date()} ({(end - start).days} days)")
    print(f"Timeframe: 1m")
    print(f"Parallel workers: 5")
    
    def fetch_one(sym):
        inst = Equity.of("NSE", sym)
        t0 = time.time()
        try:
            series = dhan.history(inst, Timeframe.M1, start, end)
            elapsed = time.time() - t0
            return sym, True, elapsed, len(series.candles)
        except Exception as e:
            elapsed = time.time() - t0
            return sym, False, elapsed, str(e)
    
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(fetch_one, sym) for sym in symbols]
        results = [f.result() for f in futures]
    total_elapsed = time.time() - t0
    
    print(f"\nTotal time: {total_elapsed:.2f}s")
    print(f"\nResults:")
    success_count = 0
    for sym, success, elapsed, detail in results:
        if success:
            print(f"  ✓ {sym}: {detail} candles in {elapsed:.2f}s")
            success_count += 1
        else:
            print(f"  ✗ {sym}: FAILED after {elapsed:.2f}s — {detail}")
    
    print(f"\nSuccess rate: {success_count}/{len(symbols)}")
    return success_count == len(symbols), total_elapsed


def run_request_spacing(spacing_seconds: float) -> bool:
    """Sequential requests with given spacing (helper, not a pytest test)."""
    print("\n" + "="*60)
    print(f"TEST 3: Sequential requests with {spacing_seconds}s spacing")
    print("="*60)
    
    dhan = load_dhan()
    symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]
    
    start = datetime(2025, 1, 1)
    end = datetime(2025, 3, 31)  # 90 days
    
    print(f"Symbols: {symbols}")
    print(f"Range: {start.date()} to {end.date()} ({(end - start).days} days)")
    print(f"Spacing: {spacing_seconds}s between requests")
    
    success_count = 0
    total_time = 0
    
    for i, sym in enumerate(symbols):
        inst = Equity.of("NSE", sym)
        t0 = time.time()
        try:
            series = dhan.history(inst, Timeframe.M1, start, end)
            elapsed = time.time() - t0
            print(f"  ✓ {sym}: {len(series.candles)} candles in {elapsed:.2f}s")
            success_count += 1
            total_time += elapsed
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  ✗ {sym}: FAILED after {elapsed:.2f}s — {e}")
        
        if i < len(symbols) - 1:
            time.sleep(spacing_seconds)
    
    print(f"\nSuccess rate: {success_count}/{len(symbols)}")
    return success_count == len(symbols)


def test_response_time():
    """Test 4: Measure actual response time for a single 90-day call."""
    print("\n" + "="*60)
    print("TEST 4: Measure response time for single 90-day 1m call")
    print("="*60)
    
    dhan = load_dhan()
    instrument = Equity.of("NSE", "RELIANCE")
    
    start = datetime(2025, 1, 1)
    end = datetime(2025, 3, 31)  # 90 days
    
    print(f"Symbol: {instrument.instrument_id}")
    print(f"Range: {start.date()} to {end.date()} ({(end - start).days} days)")
    
    # Do 3 calls and measure each
    times = []
    for i in range(3):
        t0 = time.time()
        try:
            series = dhan.history(instrument, Timeframe.M1, start, end)
            elapsed = time.time() - t0
            times.append(elapsed)
            print(f"  Call {i+1}: {elapsed:.2f}s, {len(series.candles)} candles")
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  Call {i+1}: FAILED after {elapsed:.2f}s — {e}")
            times.append(elapsed)
        
        if i < 2:
            time.sleep(2)  # Wait 2s between calls
    
    if times:
        print(f"\n  Avg: {sum(times)/len(times):.2f}s")
        print(f"  Min: {min(times):.2f}s")
        print(f"  Max: {max(times):.2f}s")


if __name__ == "__main__":
    print("Dhan Rate Limit Empirical Test")
    print("="*60)
    
    # Test 1: Single 90-day call
    test_single_90day_call()
    
    # Test 2: 5 parallel requests
    test_5_parallel_requests()
    
    # Test 3: Different spacing intervals
    for spacing in [1, 2, 3]:
        success = run_request_spacing(spacing)
        if success:
            print(f"  → {spacing}s spacing: SUCCESS")
        else:
            print(f"  → {spacing}s spacing: FAILED")
    
    # Test 4: Response time measurement
    test_response_time()
    
    print("\n" + "="*60)
    print("All tests completed")
    print("="*60)
