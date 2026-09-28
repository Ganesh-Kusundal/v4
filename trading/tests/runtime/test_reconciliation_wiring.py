"""The reconciliation monitor is wired into the session lifecycle (lane 5e).

``adopt_venue_positions`` and ``ReconciliationMonitor`` are tested in
``trading/tests/interface/test_safety_adoption.py`` and
``trading/tests/runtime/test_reconciliation_monitor.py``. Those tests prove the
monitor works. They do not prove it is *attached to anything*: before this
wiring the monitor had no production constructor, so a live session would
never have started it and venue drift would only ever be observed when an
operator asked the safety route for it.

These tests pin the attachment. Each one fails when the wiring is removed.

The monitor is injected rather than built in ``boot()`` for the same reason
``validation_report_store`` is: the seam builder
``build_reconciliation_monitor_seams`` lives in ``tradex_interfaces``, which
``tradex_runtime`` may not import (``tests/test_import_boundaries.py``).
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import replace
from unittest.mock import patch

from tradex_brokers.paper.adapter import PaperBroker
from tradex_config.schema import AppConfig
from tradex_domain import UNBOUNDED, BrokerId
from tradex_runtime.session_impl import TradingSession
from tradex_runtime.startup import boot


def _unbounded_risk(cfg: AppConfig) -> AppConfig:
    """Paper boot refuses without a named risk limit; UNBOUNDED is the opt-out."""
    return replace(
        cfg,
        risk=replace(
            cfg.risk,
            max_order_value=UNBOUNDED,
            max_position_value=UNBOUNDED,
            max_orders_per_minute=UNBOUNDED,
            max_daily_loss_amt=UNBOUNDED,
            max_drawdown_pct=UNBOUNDED,
        ),
    )


class RecordingMonitor:
    """The ``start()``/``stop()`` surface ``boot()`` and ``start()``/``stop()``
    call. Records calls so a test can assert the session owns the lifecycle."""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.started += 1
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        while self.stopped == 0:
            time.sleep(0.005)

    def stop(self) -> None:
        self.stopped += 1

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


# ---------------------------------------------------------------------------
# boot() accepts the monitor and the session owns its lifecycle
# ---------------------------------------------------------------------------


def test_boot_accepts_a_reconciliation_monitor():
    """boot() forwards the monitor to the session; a paper boot still boots."""
    monitor = RecordingMonitor()
    cfg = _unbounded_risk(AppConfig())
    session = boot(cfg, broker=PaperBroker(), wire_strategies=False,
                   reconciliation_monitor=monitor)
    try:
        # start() has run by the time boot returns: the session is READY.
        assert monitor.started == 1, "the session must start the injected monitor"
        assert monitor.alive, "the monitor's thread must be running"
    finally:
        session.stop()


def test_boot_factory_receives_the_real_components_and_starts_the_monitor():
    """A factory is invoked inside the boot with the session's own broker and
    cache, and the monitor it returns is running when boot() returns.

    This is the path cmd_serve uses. A factory assigned post-hoc would never
    start, because boot() calls session.start() before it returns.
    """
    seen = {}

    def _factory(broker, cache, engine, metrics):
        seen["broker"] = broker
        seen["cache"] = cache
        seen["engine"] = engine
        seen["metrics"] = metrics
        return RecordingMonitor()

    cfg = _unbounded_risk(AppConfig())
    broker = PaperBroker()
    session = boot(cfg, broker=broker, wire_strategies=False,
                   reconciliation_monitor_factory=_factory)
    try:
        # The factory saw the real objects boot was already building, not
        # shadows a caller had to construct separately.
        assert seen.get("broker") is broker, "the factory must receive boot's broker"
        assert seen.get("cache") is session.cache, "the factory must receive the session's book"
        assert seen.get("engine") is session.engine
        # boot() resolved the factory to a monitor and started it.
        assert session._reconciliation_monitor is not None
        assert session._reconciliation_monitor.alive
    finally:
        session.stop()


def test_session_stop_stops_the_injected_monitor():
    """stop() tears the monitor down before the broker goes."""
    monitor = RecordingMonitor()
    cfg = _unbounded_risk(AppConfig())
    session = boot(cfg, broker=PaperBroker(), wire_strategies=False,
                   reconciliation_monitor=monitor)
    session.stop()
    time.sleep(0.05)
    assert monitor.stopped == 1, "stop() must stop the injected monitor"


def test_boot_without_a_monitor_still_boots():
    """None is the default and changes nothing: the on-demand route still works."""
    cfg = _unbounded_risk(AppConfig())
    session = boot(cfg, broker=PaperBroker(), wire_strategies=False)
    session.stop()
    # No assertion on the body: reaching here means the boot and teardown both
    # succeeded with no monitor. A regression that made None load-bearing
    # would raise somewhere above.


def test_paper_factory_starts_and_stops_the_monitor():
    """TradingSession.paper() forwards the monitor to boot() and the session
    owns its lifecycle end to end."""
    monitor = RecordingMonitor()
    cfg = _unbounded_risk(AppConfig())
    session = TradingSession.paper(config=cfg, reconciliation_monitor=monitor)
    try:
        assert monitor.started == 1, "paper() must start the injected monitor"
        assert monitor.alive
    finally:
        session.stop()
    time.sleep(0.05)
    assert monitor.stopped == 1, "paper().stop() must stop the injected monitor"


def test_paper_factory_without_a_monitor_still_boots():
    """The kwarg defaults to None and changes nothing about a paper boot."""
    cfg = _unbounded_risk(AppConfig())
    session = TradingSession.paper(config=cfg)
    session.stop()


# ---------------------------------------------------------------------------
# Direct construction: the session starts and stops what it is given
# ---------------------------------------------------------------------------


def test_start_starts_the_monitor_and_stop_stops_it():
    """start()/stop() on a directly-constructed session own the monitor."""
    monitor = RecordingMonitor()
    broker = PaperBroker()
    from tradex_execution.engine import ExecutionEngine
    from tradex_execution.fill_sources import PaperFillSource
    from tradex_reactive.bus import ReactiveBus

    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=PaperFillSource())

    session = TradingSession(
        broker=broker,
        bus=bus,
        engine=engine,
        cache=engine.cache,
        broker_id=BrokerId.PAPER,
        mode="paper",
        reconciliation_monitor=monitor,
    )
    session.start()
    try:
        assert monitor.started == 1
        assert monitor.alive
    finally:
        session.stop()
    time.sleep(0.05)
    assert monitor.stopped == 1
    assert not monitor.alive


# ---------------------------------------------------------------------------
# The composition helper itself, pinned to the real seam contract
# ---------------------------------------------------------------------------


def test_build_reconciliation_monitor_is_importable_from_the_cli_root():
    """The helper is the single composition point for the two package halves.

    A test that imported only ReconciliationMonitor would not pin the thing
    that actually breaks: the seam builder and the monitor have to be joined by
    a module that can see both, and this is that module.
    """
    from tradex_interfaces.cli import build_reconciliation_monitor

    assert callable(build_reconciliation_monitor)


def test_main_boot_path_runs_the_monitor():
    """main() boots the paper session that ``tradex serve`` reuses.

    This pins the gap: cmd_serve skips its own boot for a bound runtime, so a
    monitor built only inside cmd_serve would never exist for a paper server.
    The monitor has to be built by the boot main() calls, and the way to pin
    that is to intercept boot and inspect the kwargs — the same style as
    test_cli_serve, which asserts boot is called at all.
    """
    from tradex_interfaces.cli import build_reconciliation_monitor, main

    # from_env is the only route that can carry a complete risk policy (AC-10
    # refuses to boot without one), so the test supplies the limits the way an
    # operator would. UNBOUNDED (-1) is the explicit opt-out, not a bypass.
    risk_env = {
        "TRADEX_RISK_MAX_ORDER_VALUE": "-1",
        "TRADEX_RISK_MAX_POSITION_VALUE": "-1",
        "TRADEX_RISK_MAX_ORDERS_PER_MINUTE": "-1",
        "TRADEX_RISK_MAX_DAILY_LOSS": "-1",
        "TRADEX_RISK_MAX_DRAWDOWN_PCT": "-1",
    }

    captured = {}
    real_boot = boot

    def _capture(*boot_args, **boot_kwargs):
        captured["kwargs"] = boot_kwargs
        # Fall through to a real boot so the session is genuine. main() stops
        # this session in its own finally, so capture the monitor now while
        # the session is live — after main() returns it is already stopped.
        session = real_boot(*boot_args, **boot_kwargs)
        captured["monitor"] = session._reconciliation_monitor
        captured["monitor_running"] = (
            session._reconciliation_monitor is not None
            and session._reconciliation_monitor.running
        )
        return session

    with patch("tradex_runtime.startup.boot", side_effect=_capture),\
         patch.dict(os.environ, risk_env, clear=False):
        try:
            main(["health"])
        except SystemExit:
            pass

    assert (
        captured.get("kwargs", {}).get("reconciliation_monitor_factory")
        is build_reconciliation_monitor
    ), "main() must pass the factory so the reused paper session carries a monitor"

    # The session main() built is real and carried a running monitor at READY.
    # This is the object cmd_serve reuses in paper mode, so if the monitor was
    # not running here a paper server would have no reconciliation at all.
    assert captured.get("monitor") is not None
    assert captured.get("monitor_running") is True
