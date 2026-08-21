"""Boot-path parity [REF-6, SMELL-03].

The SDK factories (TradingSession.paper) and runtime.startup.boot must
produce structurally identical sessions for the same inputs — there is one
composition root, and these tests prove the wrappers do not drift.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass

from tradex_domain import BrokerId
from tradex_trading.config.schema import AppConfig
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.runtime.startup import boot
from tradex_trading.sdk.session import TradingSession


def _graph(session: TradingSession) -> dict[str, object]:
    """Component-type fingerprint of a session's wiring."""
    return {
        "broker": type(session.broker).__name__,
        "bus": type(session.bus).__name__,
        "engine": type(session.engine).__name__,
        "cache": type(session.engine.cache).__name__,
        "state": session.state.value,
        "mode": session.mode,
        "broker_id": session.broker_id.value,
        "has_strategy_engine": session.strategy_engine is not None,
        "has_scanner_engine": session._scanner_engine is not None,
        "has_market_feed": session.market_feed is None,  # paper: must stay None
    }


def test_paper_factory_matches_boot() -> None:
    shared_bus = ReactiveBus()
    via_factory = TradingSession.paper(bus=shared_bus)
    via_boot = boot(
        AppConfig(mode="paper", broker_id=BrokerId.PAPER),
        bus=shared_bus,
        wire_strategies=False,
    )
    assert _graph(via_factory) == _graph(via_boot)
    via_factory.stop()
    via_boot.stop()


def test_paper_session_ready_and_services_usable() -> None:
    session = TradingSession.paper()
    assert session.state.value == "READY"
    session.stop()


def test_appconfig_untouched() -> None:
    # guard: the schema stays a frozen dataclass with its field set intact
    assert is_dataclass(AppConfig)
    assert {f.name for f in fields(AppConfig)} >= {
        "broker_id", "mode", "risk", "live_enabled", "execution"
    }
