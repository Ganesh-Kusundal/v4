# Progress snapshot (final E1)
- A1-A3, B1-B2, C1, C2: complete.
- D1-D3 (God Object split): complete for dhan and upstox.
- E1 (dict->domain typed surface): complete and consistent across all layers:
  * domain: added frozen OrderResult (tradex_domain.execution).
  * domain/protocols: SuperOrderAdapter + ForeverOrderAdapter return
    OrderResult / list[OrderResult].
  * brokers: DhanApiClient + UpstoxApiClient mixins parse provider dicts via
    common.client_shared.order_result_from_dict and return OrderResult.
  * brokers/common/base.py: BaseBroker super+forever pass-throughs typed
    OrderResult (was dict — fixed this session via manual check).
  * brokers/dhan/adapter.py: removed redundant super override (inherits base).
  * trading: ExtensionService super/forever/slice methods now return OrderResult
    directly; dropped local SuperOrderResult/ForeverOrderResult; re-exported
    OrderResult from domain via services/__init__.py + session.__all__.
- get_account already returns Account (domain); fund_limits/margin dicts are not
  strategy-touch surface -> left as passthrough.
- F1/F2 deferred; v3_compat.py dead to prod code, kept as deprecation shim.
- Status: 2525 passed, 2 skipped. mypy clean (54/95/16 source files per pkg).
  Ruff clean except 1 pre-existing F401 in trading/execution/fill_sources.py
  (untouched).
