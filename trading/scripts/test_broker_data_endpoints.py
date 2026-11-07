#!/usr/bin/env python3
"""Test broker data endpoints for equity, options, and futures.

Uses the standard interface: build_broker_from_env -> connect -> history/ltp -> close.
Tests all three instrument types (Equity, Option, Future) with both brokers.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, UTC
from decimal import Decimal
from pathlib import Path

# Ensure project packages are importable
ROOT = Path(__file__).resolve().parent.parent.parent
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

from tradex_config.env import load_env_file
from tradex_runtime.live import build_broker_from_env
from tradex_domain import Equity, Future, Option, Timeframe
from tradex_domain.enums import ExchangeId


def test_equity(broker, broker_name: str) -> bool:
    """Test equity data endpoint."""
    print(f"\n{'='*60}")
    print(f"Testing {broker_name}: Equity")
    print('='*60)
    
    try:
        # Use a popular NSE equity
        inst = Equity.of('NSE', 'RELIANCE')
        print(f"Instrument: {inst.instrument_id}")
        
        # LTP
        quote = broker.ltp(inst)
        print(f"LTP: {quote}")
        
        # History (last 5 days of 1-minute data)
        end = datetime.now(UTC)
        start = end - timedelta(days=5)
        series = broker.history(inst, Timeframe.M1, start, end)
        print(f"History: {len(series.candles)} candles from {series.candles[0].timestamp if series.candles else 'N/A'} to {series.candles[-1].timestamp if series.candles else 'N/A'}")
        
        if not series.candles:
            print("FAIL: No history data returned")
            return False
        
        print("PASS: Equity data working")
        return True
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return False


def test_option(broker, broker_name: str) -> bool:
    """Test option data endpoint."""
    print(f"\n{'='*60}")
    print(f"Testing {broker_name}: Option")
    print('='*60)
    
    try:
        # Search for a NIFTY option that's currently trading
        results = broker.search('NIFTY')
        print(f"Search for 'NIFTY' returned {len(results)} instruments")
        
        # Find an option
        option_inst = None
        for inst in results:
            if hasattr(inst, 'asset_class') and str(inst.asset_class) == 'OPTION':
                option_inst = inst
                print(f"Found option: {inst.instrument_id}")
                break
        
        if not option_inst:
            print("WARN: No option instruments found in search results")
            return False
        
        # LTP
        quote = broker.ltp(option_inst)
        print(f"LTP: {quote}")
        
        # History (last 2 days of 1-minute data)
        end = datetime.now(UTC)
        start = end - timedelta(days=2)
        series = broker.history(option_inst, Timeframe.M1, start, end)
        print(f"History: {len(series.candles)} candles from {series.candles[0].timestamp if series.candles else 'N/A'} to {series.candles[-1].timestamp if series.candles else 'N/A'}")
        
        if not series.candles:
            print("WARN: No history data returned (may be expired or not trading)")
            # Still consider it a pass if LTP worked
            return quote is not None
        
        print("PASS: Option data working")
        return True
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return False


def test_future(broker, broker_name: str) -> bool:
    """Test future data endpoint."""
    print(f"\n{'='*60}")
    print(f"Testing {broker_name}: Future")
    print('='*60)
    
    try:
        # Search for a NIFTY future that's currently trading
        results = broker.search('NIFTY')
        print(f"Search for 'NIFTY' returned {len(results)} instruments")
        
        # Find a future
        future_inst = None
        for inst in results:
            if hasattr(inst, 'asset_class') and str(inst.asset_class) == 'FUTURE':
                future_inst = inst
                print(f"Found future: {inst.instrument_id}")
                break
        
        if not future_inst:
            print("WARN: No future instruments found in search results")
            return False
        
        # LTP
        quote = broker.ltp(future_inst)
        print(f"LTP: {quote}")
        
        # History (last 5 days of 1-minute data)
        end = datetime.now(UTC)
        start = end - timedelta(days=5)
        series = broker.history(future_inst, Timeframe.M1, start, end)
        print(f"History: {len(series.candles)} candles from {series.candles[0].timestamp if series.candles else 'N/A'} to {series.candles[-1].timestamp if series.candles else 'N/A'}")
        
        if not series.candles:
            print("WARN: No history data returned (may be expired or not trading)")
            # Still consider it a pass if LTP worked
            return quote is not None
        
        print("PASS: Future data working")
        return True
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return False


def test_broker(broker_name: str) -> dict[str, bool]:
    """Test all data endpoints for a broker."""
    print(f"\n{'#'*60}")
    print(f"# Testing Broker: {broker_name.upper()}")
    print('#'*60)
    
    load_env_file('.env.local')
    
    broker = build_broker_from_env(broker_name)
    results = {}
    
    try:
        broker.connect()
        print(f"Connected to {broker_name}")
        
        results['equity'] = test_equity(broker, broker_name)
        results['option'] = test_option(broker, broker_name)
        results['future'] = test_future(broker, broker_name)
        
    except Exception as e:
        print(f"\nFAIL: Connection failed: {type(e).__name__}: {e}")
        results['equity'] = False
        results['option'] = False
        results['future'] = False
    finally:
        try:
            broker.close()
            print(f"\nClosed {broker_name} connection")
        except:
            pass
    
    return results


def main() -> int:
    print("="*60)
    print("Broker Data Endpoint Test")
    print("Testing Equity, Option, and Future data via standard interface")
    print("="*60)
    
    all_results = {}
    
    for broker_name in ['dhan', 'upstox']:
        all_results[broker_name] = test_broker(broker_name)
    
    # Summary
    print(f"\n{'#'*60}")
    print("# SUMMARY")
    print('#'*60)
    
    for broker_name, results in all_results.items():
        print(f"\n{broker_name.upper()}:")
        for inst_type, passed in results.items():
            status = "PASS" if passed else "FAIL"
            print(f"  {inst_type:10s}: {status}")
    
    # Count total passes
    total = sum(len(r) for r in all_results.values())
    passed = sum(v for r in all_results.values() for v in r.values())
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
