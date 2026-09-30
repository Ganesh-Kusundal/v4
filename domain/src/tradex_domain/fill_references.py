"""Single fill reference path."""

from enum import Enum


class FillReference(Enum):
    NEXT_OPEN = "next_open"
    SIGNAL_CLOSE = "signal_close"
    BOOK_MATCHING = "book_matching"
    SLIPPAGE_MODEL = "slippage_model"
