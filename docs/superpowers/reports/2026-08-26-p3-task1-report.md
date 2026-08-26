# P3 Task 1 Report — backend POST /orders/bracket endpoint

- **Status:** DONE
- **Date:** 2026-08-26
- **Commit:** `689820d1d49b5627832ca962666652fd28212bb3`

## What was done

Added the bracket (super) order HTTP surface to `trading/src/tradex_trading/interface/routes/orders.py`:

- `_build_bracket_request(body)` — mirrors `_build_order_request`'s instrument/entry parsing and adds the protective legs. `price`, `stop_loss_price`, `target_price` are required (`body["..."]` → `KeyError` when missing → 422); `trailing_jump` is optional and defaults to `Price(Decimal("0"))`. Prices are wrapped via a local `_price()` helper that converts `decimal.InvalidOperation`/`ValueError` into `ValueError` so malformed decimals surface as 422 rather than an unhandled exception.
- `place_bracket_order` — `POST /orders/bracket`, gated on `Depends(verify_api_key)`, delegates to `broker.submit_super_order(request)` and returns `{"order_id": str(order_id)}`. Matches the existing `/orders` POST exactly: `session is None` → 503 "no session bound"; `broker` missing → 503 "broker unavailable"; capability check on `capabilities.supports_super_order` → 422 when unsupported; `KeyError`/`ValueError` → 422.

No changes to the existing `/orders` routes, `_build_order_request`, the domain `OrderRequest`, or the broker facades.

## How the session exposes the broker

`TradingSession` (`trading/src/tradex_trading/sdk/session.py`) stores the broker as the **private** `self._broker` (line 94) but ALSO exposes a **public read-only `broker` property** (lines 263-266) returning that same attribute. The endpoint resolves via `getattr(session, "_broker", None) or getattr(session, "broker", None)` — `_broker` wins when present, the public property is the fallback. This covers both the real session and any lightweight fake.

## Fake-session override used in the test

`get_session` (`routes/deps.py:10`) reads `request.app.state.session`, which `create_app(session=...)` sets on the app (`fastapi_app.py:73`). So the fake is injected by passing it straight to `create_app` — no `Depends` override needed:

```python
class _FakeCaps:
    supports_super_order = True

class _FakeBroker:
    capabilities = _FakeCaps()
    def __init__(self) -> None:
        self.submitted: list[Any] = []
    def submit_super_order(self, request: Any) -> OrderId:
        self.submitted.append(request)
        return OrderId(value="bracket-1")

class _FakeSession:
    def __init__(self) -> None:
        self._broker = _FakeBroker()

session = _FakeSession()
client = TestClient(create_app(session=session))
```

The fake session carries the broker as private `_broker` (same shape as the real session), so the endpoint's `_broker` resolution path is exercised. The fake broker's `submit_super_order` records the request and returns a real domain `OrderId("bracket-1")` whose `str()` yields `"bracket-1"`.

Note on the "missing protective prices" test: it must run against the fake session (not `session=None`) because the session gate (503) precedes body validation — the 422 path is only reachable once a supporting broker is bound.

## Test output

```
$ ../.venv/bin/python -m pytest trading/tests/interface/test_bracket_order.py -q
...                                                                      [100%]
3 passed in 0.42s

$ ../.venv/bin/python -m pytest trading/tests/analytics/ -q
........................................................................ [ 22%]
........................................................................ [ 44%]
........................................................................ [ 66%]
........................................................................ [ 88%]
....................................                                     [100%]
324 passed in 0.52s
```

Red phase before implementation: all 3 new tests failed with `405 Method Not Allowed` (route absent).

Also ran the order/interface-relevant files to confirm no regression in the touched router:
`tests/interface/test_fastapi_app.py tests/interface/test_interface_modules.py` → **94 passed**.

## Ruff output

```
$ ../.venv/bin/python -m ruff check tests/interface/test_bracket_order.py
All checks passed!

$ ../.venv/bin/python -m ruff check src/tradex_trading/interface/routes/orders.py
E501 Line too long ... src/tradex_trading/interface/routes/orders.py:237
E501 Line too long ... src/tradex_trading/interface/routes/orders.py:280
```

The two `E501` findings in `orders.py` are on the **pre-existing** `@router.put(...)` / `@router.delete(...)` decorator lines (verified by ruffing the pre-change file via `git stash` — identical findings on lines 142/185 in the committed version). They are outside this task's scope (constraint: do not change existing routes) and were left untouched.

## Commit

```
git add trading/src/tradex_trading/interface/routes/orders.py trading/tests/interface/test_bracket_order.py
git commit -m "feat(api): POST /orders/bracket — super-order placement"
```

Staged exactly those two files.