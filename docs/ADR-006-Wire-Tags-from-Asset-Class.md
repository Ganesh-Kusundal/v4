# ADR-006: Provider-Key Wire Tags Derived from Asset Class

## Status

Accepted — 2026-08-12

## Decision

The provider-key wire tag is derived from the instrument's asset class instead
of from F&O right/strike fields. `InstrumentRegistry._tag_from_id`
(`domain/src/tradex_domain/wire.py:141`) now looks the tag up in the
`_TAG_BY_ASSET_CLASS` table (`wire.py:30`):

| Asset class | Tag |
|-------------|-----|
| `EQUITY`    | `EQ` |
| `INDEX`     | `IDX` |
| `FUTURE`    | `FUT` |
| `OPTION`    | `OPT` |
| `CURRENCY`  | `CUR` |
| `COMMODITY` | `COM` |
| `ETF`       | `ETF` |

Any asset class without an explicit mapping falls back to `EQ`
(`wire.py:146`). The tag feeds the canonical key format
`{exchange}_{tag}|{underlying}{:expiry}{:strike}{:right}` built by `_key`
(`wire.py:149`), so `InstrumentId.index("NSE", "NIFTY")` maps to
`NSE_IDX|NIFTY`, `InstrumentId.currency("NSE", "USDINR")` to `NSE_CUR|USDINR`,
and `InstrumentId.commodity("MCX", "GOLD")` to `MCX_COM|GOLD` (asserted in
`TestProviderKeyTagByAssetClass`, `domain/tests/test_registry_key_semantics.py:211`).

For rows loaded from a master file, `_default_key` (`wire.py:160`) prefers the
row's declared `asset_class` metadata tag and falls back to
`_tag_from_id` for rows that lack it — a legacy master without asset-class
metadata still produces the same deterministic tag its `InstrumentId` would.
Previously-registered keys are preserved as aliases by the incremental
registration path, and an authoritative master reload re-points stale keys to
the new asset-class tag for instruments the fresh master defines (see
ADR-003).

## Context

The old `_tag_from_id` mapped only from F&O fields: `right == "FUT"` → `FUT`,
`right in {"CE", "PE"}` → `OPT`, everything else → `EQ`. Index, currency, and
commodity instruments carry no F&O right, so they all received the `EQ` tag:
`NSE_EQ|NIFTY`, `NSE_EQ|USDINR`, and `MCX_EQ|GOLD` could not be distinguished
from equity keys, producing ambiguous or colliding canonical keys for
unrelated asset classes sharing a symbol/underlying. The tag must encode the
full asset class so the canonical key round-trips reversibly (ADR-003) for
every instrument kind.

## Consequences

### Positive

- Index/currency/commodity/ETF instruments get distinct canonical keys, fixing
  the cross-asset-class ambiguity and collision hazard.
- Keys stay reversible and deterministic for the whole instrument surface, not
  just equity + F&O.
- `_default_key`'s metadata-first, `_tag_from_id` fallback keeps legacy master
  rows (no `asset_class` column) resolving to stable keys without a migration.
- No behavioral change for equity and F&O instruments — their tags are
  unchanged (`EQ`, `FUT`, `OPT`).

### Negative

- The tag table must stay in sync with any new `AssetClass` enum member; a
  missing entry silently falls back to `EQ` rather than failing loudly.
- Persisted keys generated under the old scheme (e.g. `NSE_EQ|NIFTY`) differ
  from freshly derived keys (`NSE_IDX|NIFTY`) until the next authoritative
  master reload re-points them.
- `EQ` as the catch-all default masks tag gaps — an unmapped asset class is
  indistinguishable from equity until its key is inspected.
- The tag is a string convention with no schema enforcement, so a future
  re-tagging would break key reversibility for persisted state (inherited from
  ADR-003's key format).
