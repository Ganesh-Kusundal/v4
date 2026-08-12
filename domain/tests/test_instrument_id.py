"""InstrumentId asset-class discriminator (residual review Task 1)."""

from datetime import date

from tradex_domain.enums import AssetClass
from tradex_domain.value_objects import InstrumentId


def test_factories_set_asset_class():
    assert InstrumentId.equity("NSE", "TCS").asset_class is AssetClass.EQUITY
    assert InstrumentId.index("NSE", "NIFTY").asset_class is AssetClass.INDEX
    assert InstrumentId.currency("NSE", "USDINR").asset_class is AssetClass.CURRENCY
    assert InstrumentId.commodity("MCX", "GOLD").asset_class is AssetClass.COMMODITY
    assert InstrumentId.future("NSE", "TCS", date(2026, 8, 27)).asset_class is AssetClass.FUTURE
    assert InstrumentId.option("NSE", "TCS", date(2026, 8, 27), 3000, "CE").asset_class is AssetClass.OPTION


def test_equity_and_index_carry_distinct_asset_class():
    # Same exchange+underlying with different asset classes must differ in the
    # advisory asset_class hint (used for provider-key tag generation), while
    # remaining equal as a registry key.
    assert InstrumentId.equity("NSE", "TCS").asset_class is AssetClass.EQUITY
    assert InstrumentId.index("NSE", "TCS").asset_class is AssetClass.INDEX
    assert InstrumentId.equity("NSE", "TCS") == InstrumentId.index("NSE", "TCS")


def test_parse_backfills_asset_class():
    assert InstrumentId.parse("NSE:TCS").asset_class is AssetClass.EQUITY
    assert InstrumentId.parse("NSE:TCS:20260827:3000:CE").asset_class is AssetClass.OPTION
    assert InstrumentId.parse("NSE:TCS:20260827:FUT").asset_class is AssetClass.FUTURE
