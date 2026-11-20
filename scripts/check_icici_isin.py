#!/usr/bin/env python3
"""Check ISIN mapping for ICICI life insurance symbols."""

import sys
from pathlib import Path

# Add project roots to path
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "brokers" / "src"))
sys.path.insert(0, str(project_root / "domain" / "src"))

from tradex_runtime.live import build_broker_from_env
from tradex_domain.value_objects import InstrumentId

def main():
    # Build broker
    broker = build_broker_from_env('dhan')
    broker.connect()

    print('ISIN comparison:')
    print('=' * 80)
    
    # Check ICICILIFE in broker
    icici_life_iid = InstrumentId.equity('NSE', 'ICICILIFE')
    try:
        provider_key = broker.registry.provider_key(icici_life_iid)
        meta = broker.registry.meta(icici_life_iid)
        isin = meta.get('isin', 'N/A')
        print(f'Broker - ICICILIFE:')
        print(f'  Provider key: {provider_key}')
        print(f'  ISIN: {isin}')
    except Exception as e:
        print(f'Broker - ICICILIFE: Error - {e}')
    
    # Check from universe CSV
    from tradex_domain.universe import load_universe
    universe = load_universe('nifty500', csv_dir=None)
    
    print('\nUniverse CSV:')
    for inst in universe:
        if inst.symbol in ['ICICIPRULI', 'ICICILIFE']:
            isin = getattr(inst.meta, 'isin', 'N/A') if hasattr(inst, 'meta') else 'N/A'
            print(f'  {inst.symbol:20s} - ISIN: {isin}')
    
    print('\n' + '=' * 80)
    print('Conclusion:')
    print('  ICICIPRULI (universe CSV) likely maps to ICICILIFE (broker symbol)')
    print('  This is a symbol rename that needs to be handled.')

if __name__ == '__main__':
    main()
