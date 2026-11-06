"""Test doubles for the gateway suite."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.enums import AssetClass, Timeframe
from tradex_domain.instruments import Equity, Index, Instrument, Option
from tradex_domain.market import OHLC, Candle, Depth, HistoricalSeries, Quote
from tradex_domain.options import Expiry, OptionChain, OptionPair
from tradex_domain.value_objects import InstrumentId, Price, Quantity
from tradex_gateway.config import GatewayConfig
from tradex_market_data.window_loader import MarketSource, MarketWindow


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRegistry:
    def __init__(
        self,
        mapping: dict[str, InstrumentId] | None = None,
        lot_sizes: dict[str, object] | None = None,
    ) -> None:
        self.mapping = mapping or {}
        self.lot_sizes = lot_sizes or {}

    def resolve(self, name: str) -> InstrumentId | None:
        if name in self.mapping:
            return self.mapping[name]
        try:
            return InstrumentId.parse(name)
        except ValueError:
            return None

    def meta(self, instrument_id: InstrumentId) -> dict[str, object]:
        value = self.lot_sizes.get(str(instrument_id))
        return {"lot_size": value} if value is not None else {}


def equity(symbol: str = "RELIANCE") -> Equity:
    return Equity.of("NSE", symbol)


def index(symbol: str = "NIFTY") -> Index:
    return Index.of("IDX", symbol)


def option(
    strike: float = 25000,
    right: str = "CE",
    expiry: date | None = None,
) -> Option:
    return Option.of("NFO", "NIFTY", expiry or date.today() + timedelta(days=7), strike, right)


def make_candle(
    instrument: Instrument,
    moment: datetime,
    price: float = 100.0,
    volume: float = 10.0,
) -> Candle:
    level = Price(Decimal(str(price)))
    return Candle(
        instrument=instrument,
        timeframe=Timeframe.M1,
        ohlc=OHLC(open=level, high=level, low=level, close=level),
        volume=Quantity(Decimal(str(volume))),
        timestamp=moment,
    )


def make_quote(
    instrument: Instrument,
    moment: datetime | None = None,
    ltp: float = 100.0,
) -> Quote:
    level = Price(Decimal(str(ltp)))
    return Quote(
        instrument=instrument,
        ltp=level,
        bid=level,
        ask=Price(Decimal(str(ltp + 0.05))),
        volume=Quantity(Decimal("1000")),
        open_interest=Quantity(Decimal("500")),
        timestamp=moment or datetime.now(UTC),
    )


def make_depth(instrument: Instrument, levels: int = 5) -> Depth:
    bids = tuple(
        (Price(Decimal(str(100 - step))), Quantity(Decimal(str(10 + step))))
        for step in range(levels)
    )
    asks = tuple(
        (Price(Decimal(str(101 + step))), Quantity(Decimal(str(10 + step))))
        for step in range(levels)
    )
    return Depth(instrument=instrument, bids=bids, asks=asks, timestamp=datetime.now(UTC))


def make_chain(underlying: Instrument, expiry: date | None = None) -> OptionChain:
    expiry_date = expiry or date.today() + timedelta(days=7)
    call = Option.of("NFO", "NIFTY", expiry_date, 25000, "CE")
    put = Option.of("NFO", "NIFTY", expiry_date, 25000, "PE")
    pair = OptionPair(call=call, put=put, strike=Price(Decimal("25000")))
    expiry_obj = Expiry(
        underlying=underlying,
        expiry_date=expiry_date,
        pairs=(pair,),
        reference_price=Price(Decimal("25000")),
    )
    return OptionChain(underlying=underlying, _expiries=(expiry_obj,))


class FakeBroker:
    def __init__(
        self,
        name: str = "dhan",
        *,
        depth_levels: int | None = None,
        supports_batch: bool = True,
        universe: list[Instrument] | None = None,
    ) -> None:
        self.name = name
        self.registry = FakeRegistry()
        self.capabilities = BrokerCapabilities(
            supports_batch_market_data=supports_batch,
            supports_option_chain=True,
            supports_future_chain=True,
            depth_levels=(
                depth_levels if depth_levels is not None else (20 if name == "dhan" else 30)
            ),
            max_stream_instruments=1000 if name == "dhan" else 500,
            supported_asset_classes=(
                AssetClass.EQUITY,
                AssetClass.INDEX,
                AssetClass.FUTURE,
                AssetClass.OPTION,
            ),
        )
        self.calls: list[tuple] = []
        self.history_impl = None
        self.quote_value: Quote | None = None
        self.chain_value: OptionChain | None = None
        self.futures: list[Instrument] = []
        self.quote_handlers: list = []
        self.depth_handlers: list = []
        self.quote_sub_calls = 0
        self.depth_sub_calls = 0
        self.fail_subscribe = False
        self.unsubscribed: list = []
        self.universe = universe or [equity(), index(), option(), option(right="PE")]

    def _universe(self):
        return list(self.universe)

    def search(self, query: str):
        needle = query.strip().upper()
        return [item for item in self.universe if needle in item.symbol.upper()]

    def history(self, instrument, timeframe=None, start=None, end=None, **kwargs):
        self.calls.append(("history", str(instrument.instrument_id), str(timeframe)))
        if self.history_impl is not None:
            return self.history_impl(instrument, timeframe, start, end)
        return HistoricalSeries(
            instrument=instrument,
            timeframe=timeframe,
            candles=[],
            start=start,
            end=end,
        )

    def get_quote(self, instrument):
        self.calls.append(("get_quote", str(instrument.instrument_id)))
        return self.quote_value or make_quote(instrument)

    def quote_batch(self, instruments):
        self.calls.append(("quote_batch", [str(item.instrument_id) for item in instruments]))
        return {item.instrument_id: (self.quote_value or make_quote(item)) for item in instruments}

    def depth(self, instrument):
        self.calls.append(("depth", str(instrument.instrument_id)))
        return make_depth(instrument)

    def get_option_chain(self, underlying, expiry=None):
        self.calls.append(("option_chain", str(underlying.instrument_id), expiry))
        return self.chain_value or make_chain(underlying, expiry)

    def future_chain(self, underlying):
        self.calls.append(("future_chain", str(underlying.instrument_id)))
        return self.futures

    def subscribe_quotes(self, instruments, handler):
        if self.fail_subscribe:
            raise RuntimeError("websocket unavailable")
        self.quote_sub_calls += 1
        self.quote_handlers.append(handler)
        return f"{self.name}-quotes-{self.quote_sub_calls}"

    def subscribe_depth(self, instruments, handler):
        self.depth_sub_calls += 1
        self.depth_handlers.append(handler)
        return f"{self.name}-depth-{self.depth_sub_calls}"

    def subscribe_depth_30(self, instruments, handler):
        self.depth_sub_calls += 1
        self.depth_handlers.append(handler)
        return f"{self.name}-depth30-{self.depth_sub_calls}"

    def unsubscribe_instruments(self, instruments):
        self.unsubscribed.extend(str(item.instrument_id) for item in instruments)

    def unsubscribe(self, subscription):
        self.calls.append(("unsubscribe", subscription))

    def close(self):
        self.calls.append(("close",))


class FakeWindowLoader:
    def __init__(
        self,
        candles: tuple[Candle, ...] = (),
        source: MarketSource = MarketSource.DATALAKE,
    ) -> None:
        self.candles = candles
        self.source = source
        self.calls = 0

    def load(self, instrument, timeframe, start, end, *, allow_broker=False):
        self.calls += 1
        return MarketWindow(
            instrument=instrument,
            timeframe=timeframe,
            start=start,
            end=end,
            candles=self.candles,
            source=self.source,
        )


def gateway_config(**overrides) -> GatewayConfig:
    values = {
        "api_key": None,
        "allow_anon": True,
        "brokers": ("dhan", "upstox"),
        "primary_live": "dhan",
        "primary_quotes": "upstox",
        "primary_history": "upstox",
        "primary_depth": "dhan",
        "primary_chain": "dhan",
        "stale_after_seconds": 3600.0,
        "rest_rate_per_second": 1000.0,
        "rest_burst": 1000,
        "max_subscriptions": 100,
    }
    values.update(overrides)
    return GatewayConfig(**values).validate()
