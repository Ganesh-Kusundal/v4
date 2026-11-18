# Greeks leg dicts + review fixes — Implementation Plan

**Goal:** Align the uncommitted option-chain greeks work with spec
`docs/superpowers/specs/2026-11-12-option-greeks-iv-exposure-design.md` and fix
the correctness findings from the two-axis review. WS modes, tick_size, and
Dhan WS changes stay (user-approved extra scope).

**Spec:** `docs/superpowers/specs/2026-11-12-option-greeks-iv-exposure-design.md`

## Global constraints

- Leg dicts are plain JSON-able dicts, normalized keys per spec table, `None`
  values omitted, leg is `None` when nothing parseable. Never raises.
- `OptionPair` serialized output unchanged when legs unset ("emitted only when
  set").
- Gateway `ce`/`pe` entries gain `"leg"` only when set — byte-compatible
  otherwise. Interfaces pairs gain `call_leg`/`put_leg` only when set.
- Live overlay touches `ltp`/`oi`/`volume` only; never adds greeks; never
  erases a leg value with `None`.
- No test deleted/weakened; existing suite stays green. One commit at the end.

## Tasks

### 1. Domain: dict legs on OptionPair

- `domain/src/tradex_domain/options.py`: delete `OptionLegMeta`; `OptionPair`
  gains `call_leg: dict[str, object] | None = None`, `put_leg: ... = None`.
  Override `to_dict()` to drop the two keys when `None`. `__all__` drops
  `OptionLegMeta`.

### 2. Shared leg-dict helper + broker parsers

- New `brokers/src/tradex_brokers/common/option_legs.py`: `opt_float(value)`
  and `option_leg(provider, fields) -> dict | None` (drops Nones, adds
  `provider`, `None` when empty).
- `dhan/_marketdata.py`: `_dhan_leg_meta` → `_dhan_leg` returning the spec
  table dict (ltp/oi/volume/previous_oi/previous_volume/bid/ask/bid_qty/
  ask_qty/iv/greeks{delta,gamma,theta,vega}/provider="dhan").
- `upstox/_marketdata.py`: same for `option_greeks`/`market_data` sources,
  `pop` passthrough, provider="upstox", no `previous_volume`.
- Call sites pass `call_leg=`/`put_leg=`.

### 3. Empty-chain detection

- `gateway/market.py`: `_chain_has_pairs(chain)` =
  `any(e.pairs for e in chain.expiries())`; use in `_fetch` retry check.
- `dhan/adapter.py`: empty-REST check `not chain.expiries()` →
  `not _chain_has_pairs`-equivalent inline `any(...)`.

### 4. Gateway market.py

- `_serialize_leg(option, leg_dict, lot_size_for)`: emit `"leg": dict(leg)`
  when set.
- `serialize_chain`: use `pair.call_leg`/`pair.put_leg`.
- `_enrich`: revert bid/ask; overlay `{ltp,oi,volume}` (skip `None`) into
  `entry["leg"]` when present else top-level (pre-diff behavior).
- `_call_option_chain`: direct call with `allow_master_fallback=False`;
  add the kwarg to `BrokerAdapter.get_option_chain` (protocols.py) and
  `paper/adapter.py`; update `FakeBroker.get_option_chain` signature.

### 5. Interfaces app

- `contracts.py OptionChainResponse`: `underlying`, `live=False`, `expiry`,
  `expiries`, `strikes`, `enriched=False` per spec §5.
- `_market_helpers.serialize_option_chain`: emit `call_leg`/`put_leg` when set.
- `enrich_chain_live`: when `p.call_leg`/`p.put_leg` set, emit merged
  `call_leg`/`put_leg` (leg baseline + live ltp/oi/volume overrides, strings
  per interface convention); absent → current `call_live`/`put_live` path.

### 6. live.py hub fixes

- `_BrokerFeed.sync_option_greeks`: on any set change, unsubscribe the whole
  greeks subscription and re-subscribe the new set (no
  `unsubscribe_instruments` on the shared quote refcount path).
- `_BrokerFeed.has_depths()`; `_sync_feed` uses it instead of `feed._depths`.
- `_prune_state`: `agg.flush()` before dropping an aggregator; `stop()` flushes
  all aggregators.
- `_Subscription` dataclass (mode/levels/timeframe) and `_Wanted` dataclass
  replace the 3-tuple and 5-tuple.

### 7. Dhan ws_streams + tick_parser

- `tick_parser.py`: public `parse_tick_frames(buf) -> list[dict] | None`
  (None = not a binary frame; else parsed rows for each complete packet,
  stopping at truncation).
- `ws_streams.py`: use it instead of importing `_MSG_TYPES`; name the
  subscribe chunk limit (`_MAX_SUBSCRIBE_BATCH = 100`).

### 8. Tests

- Rewrite `gateway/tests/test_chain_greeks_failover.py` for dict legs +
  `"leg"` payload.
- `brokers/tests/test_option_chain_legs.py` (new): Dhan+Upstox fixture
  payloads → normalized dicts incl. `pop`, `provider`, None-omission.
- `test_dhan_adapter.py`: REST chain with expiry-but-no-pairs → master
  fallback.
- `test_market.py`: empty-pairs chain triggers the None-expiry retry.
- `test_stream_greeks_bars.py` / `test_live.py`: greeks resubscribe-on-change,
  bar flush on unsubscribe/stop, multi-packet frame test (broker side).
- Interfaces: existing 3 option-chain tests go green; add one OC-leg merge
  assertion if cheap.

### 9. Verify + commit

- Focused pytest runs per package, then full suite
  `uv run --no-sync python -m pytest -c pyproject.toml -q -p no:cacheprovider`.
- Ensure runtime artifacts (`data/.sync.lock`, `*.backup`,
  `recent-changes.diff`) are not staged. One commit.
