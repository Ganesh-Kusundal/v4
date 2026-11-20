#!/usr/bin/env python3
"""Investigate ICICIPRULI provider key issue."""

import sys
from pathlib import Path

# Add project roots to path
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "brokers" / "src"))
sys.path.insert(0, str(project_root / "domain" / "src"))

from tradex_domain.instruments import Equity
from tradex_domain.value_objects import InstrumentId
from tradex_runtime.live import build_broker_from_env

def main():
    # Build broker
    broker = build_broker_from_env('dhan')
    broker.connect()

    # Try to resolve ICICIPRULI
    symbol = 'ICICIPRULI'
    iid = InstrumentId.equity('NSE', symbol)

    print(f'Symbol: {symbol}')
    print(f'Instrument ID: {iid}')
    print(f'Exchange: {iid.exchange}')
    print()

    # Check registry
    try:
        provider_key = broker.registry.provider_key(iid)
        print(f'Provider key: {provider_key}')
    except Exception as e:
        print(f'Error getting provider key: {type(e).__name__}: {e}')

    # Check if symbol exists in loaded instruments
    loaded_symbols = [inst.symbol for inst in broker._loaded_instruments]
    print(f'\nTotal loaded instruments: {len(loaded_symbols)}')
    print(f'ICICIPRULI in loaded instruments: {symbol in loaded_symbols}')

    # Search for similar symbols
    similar = [s for s in loaded_symbols if 'ICICI' in s]
    print(f'\nSimilar symbols with "ICICI": {similar}')

    # Check if there's a mapping in the CSV
    from tradex_domain.universe import load_universe
    try:
        universe = load_universe('nifty500', csv_dir=None)
        icici_instruments = [inst for inst in universe if 'ICICI' in inst.symbol]
        print(f'\nICICI instruments in nifty500 universe:')
        for inst in icici_instruments:
            print(f'  {inst.symbol} - ISIN: {getattr(inst.meta, "isin", "N/A") if hasattr(inst, "meta") else "N/A"}')
    except Exception as e:
        print(f'\nError loading universe: {e}')

if __name__ == '__main__':
    main()
