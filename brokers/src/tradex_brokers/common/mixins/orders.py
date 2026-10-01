"""Orders: deliberate decision record — NO shared mixin (REF-8).

Every orders method was compared pairwise between
``tradex_brokers.dhan._orders.OrdersMixin`` and
``tradex_brokers.upstox._orders.OrdersMixin`` and KEPT PER-BROKER. The two
providers differ in endpoint, host routing (Upstox HFT/v3), payload shape and
response unwrapping, so a shared body would be either a misleading
abstraction or a wire-format hazard:

- ``submit_order`` / ``cancel_order`` / ``modify_order`` / ``get_order`` /
  ``get_orderbook``: different endpoints and id extraction (Dhan
  ``/orders[/{id}]`` with ``orderId``/``order_id``; Upstox ``/order/*`` on the
  HFT host with ``order_id``/``order_ids``).
- ``get_order_by_correlation_id``: Dhan has a native external-id lookup
  (``/orders/external/{id}``); Upstox scans the full order book for a
  matching tag.
- ``submit_super_order``: Dhan implements ``POST /super/orders`` with
  protective-price validation; Upstox raises ``CapabilityNotSupportedError``
  (a per-provider capability fact, not a parameterizable gate).
- ``submit_forever_order`` / ``modify_forever_order`` /
  ``cancel_forever_order`` / ``list_forever_orders``: Dhan
  ``/forever/orders`` payloads (``orderFlag``/``triggerPrice``/``price``) vs
  Upstox GTT ``/order/gtt/*`` payloads (``rules[]``); Upstox has no
  list-all GTT endpoint and returns ``[]``.
- ``submit_slice_order``: Dhan server-side slicing (``/orders/slicing``)
  vs Upstox client-side quantity splitting.

Upstox ``submit_edis`` (a ``CapabilityNotSupportedError`` stub) also stays in
``upstox/_orders.py`` next to the eDIS capability asymmetry.

If a future provider reaches wire parity with an existing body, move the
method into a shared mixin in this package.
"""
