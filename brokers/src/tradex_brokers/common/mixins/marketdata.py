"""Market data: deliberate decision record — NO shared mixin (REF-8).

Compared pairwise between ``tradex_brokers.dhan._marketdata.MarketDataMixin``
and ``tradex_brokers.upstox._marketdata.MarketDataMixin`` and KEPT PER-BROKER.
Request shapes and response envelopes differ end to end (Dhan: POST
segment-map payloads on ``/marketfeed/*``; Upstox: GET instrument-key queries
on the v3 host):

- ``ltp`` / ``ltp_batch`` / ``quote_batch`` / ``get_quote``: different
  endpoints, batching strategies (Dhan segment map vs Upstox 500-key
  chunking) and instrument-key resolution.
- ``depth``: only the final ``build_depth`` tail is shared; the fetch and
  row extraction differ entirely, so the method is not worth splitting.
- ``history``: Dhan intraday/historical chart endpoints with the
  exclusive-``fromDate`` lead-bar fix vs Upstox v3 candle paths with the
  same-IST-day intraday route.
- ``get_option_chain``: Dhan expiry-list + chain POST flow vs Upstox single
  ``GET /option/chain`` with explicit-expiry requirement.

Per-provider extras also stay put: Dhan ``_validated`` failure-body gate;
Upstox ``get_ohlc`` / ``intraday_candles`` / ``get_news``.

If a future provider reaches wire parity with an existing body, move the
method into a shared mixin in this package.
"""
