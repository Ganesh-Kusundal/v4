"""Protective trigger checks against a single candle."""

from tradex_domain.enums import OrderSide
from tradex_domain.market import Candle
from tradex_domain.value_objects import Price


def stop_hit(candle: Candle, stop: Price, side: OrderSide) -> bool:
    if side is OrderSide.BUY:
        return candle.ohlc.low.value <= stop.value
    return candle.ohlc.high.value >= stop.value


def target_hit(candle: Candle, target: Price, side: OrderSide) -> bool:
    if side is OrderSide.BUY:
        return candle.ohlc.high.value >= target.value
    return candle.ohlc.low.value <= target.value
