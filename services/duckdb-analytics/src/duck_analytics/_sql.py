"""Shared SQL literal helpers — single source for quoting and IN-list building."""

from __future__ import annotations


def quote_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def symbol_in_list(symbols: list[str]) -> str:
    if not symbols:
        return "AND false"
    return "AND symbol IN (" + ", ".join(quote_literal(s) for s in symbols) + ")"


__all__ = ["quote_literal", "symbol_in_list"]
