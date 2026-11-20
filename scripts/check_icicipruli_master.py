#!/usr/bin/env python3
"""Check if ICICIPRULI exists in broker master data."""

import sys
from pathlib import Path

# Add project roots to path
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "brokers" / "src"))
sys.path.insert(0, str(project_root / "domain" / "src"))

from tradex_runtime.live import build_broker_from_env


def main():
    # Build broker
    broker = build_broker_from_env('dhan')
    broker.connect()

    # Search for ICICIPRULI in loaded instruments
    target_symbol = 'ICICIPRULI'

    print(f'Searching for {target_symbol} in broker master...')
    print('=' * 80)

    # Check exact match
    exact_matches = [inst for inst in broker._loaded_instruments if inst.symbol == target_symbol]
    print(f'\nExact matches for "{target_symbol}": {len(exact_matches)}')

    # Check partial matches
    partial_matches = [inst for inst in broker._loaded_instruments if 'ICICIPRULI' in inst.symbol or 'PRUDENTIAL' in inst.symbol]
    print(f'Partial matches (containing ICICIPRULI or PRUDENTIAL): {len(partial_matches)}')

    if partial_matches:
        print('\nFound instruments:')
        for inst in partial_matches[:10]:
            iid = inst.instrument_id
            meta = broker.registry.meta(iid)
            isin = meta.get('isin', 'N/A')
            print(f'  {inst.symbol:20s} - Exchange: {iid.exchange:5s} - ISIN: {isin}')

    # Check all ICICI-related equity instruments on NSE
    print('\n' + '=' * 80)
    print('All NSE equity instruments with "ICICI" in symbol:')
    icici_nse = [
        inst for inst in broker._loaded_instruments
        if inst.symbol.startswith('ICICI') and hasattr(inst, 'exchange') and str(getattr(inst, 'exchange', '')) == 'NSE'
    ]

    unique_symbols = sorted(set(inst.symbol for inst in icici_nse))
    print(f'Unique symbols: {len(unique_symbols)}')
    for sym in unique_symbols:
        print(f'  - {sym}')

    # Check if there's a different symbol name for ICICI Prudential Life Insurance
    print('\n' + '=' * 80)
    print('Searching for life insurance companies...')
    life_insurance = [
        inst for inst in broker._loaded_instruments
        if 'LIFE' in inst.symbol and hasattr(inst, 'exchange') and str(getattr(inst, 'exchange', '')) == 'NSE'
    ]
    unique_life = sorted(set(inst.symbol for inst in life_insurance))
    print(f'Life insurance symbols on NSE: {len(unique_life)}')
    for sym in unique_life[:20]:
        print(f'  - {sym}')

if __name__ == '__main__':
    main()
