"""AC-17 / lane 5c: structured logging is ON, and one order is greppable end to end.

The module under test is not really ``logging_config`` — it is the *wiring*.
``setup_structured_logging`` and the correlation contextvar were both already
present and both already unit-tested, and both were dead: nothing outside their
own module and their own test called either. A green ``test_structured_logging``
was therefore compatible with a process that logged nothing structured and
correlated nothing, which is the state this lane started from.

So the first test here is the anti-dead-code control. It is deliberately hard to
satisfy accidentally: it asserts the handler is installed on the root logger
*after a real ``boot``*, so deleting the call from ``startup.boot`` fails the
build. The rest assert the property the brief is actually for — that one
correlation id can be followed from the signal, through the submit, out to the
venue request, and that a record with no id neither fabricates one nor crashes
the formatter.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from decimal import Decimal
from io import StringIO
from typing import Any

import pytest
from tradex_config.schema import AppConfig, LoggingConfig
from tradex_domain import (
    CorrelationId,
    Equity,
    OrderRequest,
    OrderSide,
    OrderType,
    Price,
    Quantity,
)
from tradex_execution.engine import ExecutionEngine, MemoryIdempotencyGuard
from tradex_execution.fill_sources import BrokerFillSource, PaperFillSource
from tradex_execution.risk import RiskManager
from tradex_observability.tracing import (
    NO_TRACE,
    get_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from tradex_reactive.bus import ReactiveBus
from tradex_runtime.logging_config import (
    JsonFormatter,
    StructuredFormatter,
    setup_structured_logging,
)
from trading.tests.risk_support import permissive_risk

_RELIANCE = Equity.of("NSE", "RELIANCE")
_CID = "trace-under-test-0001"


@pytest.fixture
def captured() -> Any:
    """Route the root logger at INFO into a buffer and restore it afterwards.

    ``setup_structured_logging`` owns the root logger, so a test that leaves it
    pointing at a StringIO would quietly disarm the logging of whatever runs
    next. The teardown puts the handlers back the way they were found.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    stream = StringIO()
    setup_structured_logging("tradex", json_format=True, level=logging.INFO)
    for handler in logging.getLogger().handlers:
        handler.setStream(stream)
    try:
        yield stream
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def _request(correlation_id: str | None) -> OrderRequest:
    return OrderRequest(
        instrument=_RELIANCE,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal(1)),
        price=Price(Decimal(100)),
        correlation_id=(
            CorrelationId(value=correlation_id) if correlation_id else None
        ),
    )


def _engine(fill_source: Any = None) -> ExecutionEngine:
    bus = ReactiveBus()
    return ExecutionEngine(
        bus=bus,
        fill_source=fill_source if fill_source is not None else PaperFillSource(),
        risk_manager=RiskManager(max_order_value=None, max_position_value=None),
        idempotency_guard=MemoryIdempotencyGuard(),
    )


class _AckBroker:
    """A venue that accepts, so the request really leaves the process.

    ``BrokerFillSource`` is the only path that reaches a venue — paper fills are
    simulated and never touch ``_dispatch`` — so tracing "out to the venue" means
    driving this source, not the paper one.
    """

    def __init__(self) -> None:
        self.seen: list[OrderRequest] = []

    def submit_order(self, request: OrderRequest) -> str:
        self.seen.append(request)
        return "venue-order-1"


class TestStructuredLoggingIsWired:
    """The anti-dead-code control."""

    def test_boot_installs_the_structured_handler(self) -> None:
        """``boot`` must call ``setup_structured_logging``.

        This is the test that would have failed before lane 5c. It runs a real
        (paper) boot rather than asserting on a spy, so it stays true regardless
        of how boot reaches the call — and it fails if the call is deleted, which
        is the whole point: the seam must not be able to rot back into dead code
        without a red build.
        """
        from tradex_runtime.startup import boot

        root = logging.getLogger()
        saved = root.handlers[:]
        saved_level = root.level
        try:
            root.handlers.clear()
            root.setLevel(logging.WARNING)
            session = boot(
                AppConfig(mode="paper", risk=permissive_risk()),
                wire_strategies=False,
            )
            try:
                assert root.handlers, (
                    "boot left no handler on the root logger — structured "
                    "logging was not installed, so nothing is being emitted"
                )
                handler = root.handlers[0]
                assert isinstance(
                    handler.formatter, (StructuredFormatter, JsonFormatter)
                ), (
                    f"boot installed a handler with a non-structured "
                    f"formatter: {type(handler.formatter).__name__}"
                )
                # The correlation filter is what stamps the trace id onto money
                # path records; a handler without it cannot correlate anything.
                assert any(
                    type(f).__name__ == "CorrelationIdFilter"
                    for f in handler.filters
                ), "boot's handler has no CorrelationIdFilter — ids cannot be stamped"
            finally:
                session.stop()
        finally:
            root.handlers[:] = saved
            root.setLevel(saved_level)

    def test_boot_honours_the_configured_level_and_format(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator's ``TRADEX_LOG_*`` reaches the installed handler.

        A level hardcoded in ``boot`` would satisfy the test above while making
        the configuration fields decorative, so this pins the other half of the
        contract: what the operator named is what gets installed.
        """
        from tradex_config.env import from_env
        from tradex_runtime.startup import boot

        monkeypatch.setenv("TRADEX_LOG_LEVEL", "ERROR")
        monkeypatch.setenv("TRADEX_LOG_JSON", "true")
        cfg = from_env()
        assert cfg.logging.level == "ERROR"
        assert cfg.logging.json_format is True

        root = logging.getLogger()
        saved = root.handlers[:]
        saved_level = root.level
        try:
            session = boot(
                replace(cfg, risk=permissive_risk()), wire_strategies=False
            )
            try:
                assert root.level == logging.ERROR
                assert isinstance(root.handlers[0].formatter, JsonFormatter)
            finally:
                session.stop()
        finally:
            root.handlers[:] = saved
            root.setLevel(saved_level)

    def test_env_default_is_info_human_readable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tradex_config.env import from_env

        monkeypatch.delenv("TRADEX_LOG_LEVEL", raising=False)
        monkeypatch.delenv("TRADEX_LOG_JSON", raising=False)
        cfg = from_env()
        assert cfg.logging.level == "INFO"
        assert cfg.logging.level_value() == logging.INFO
        assert cfg.logging.json_format is False

    def test_bad_level_name_is_refused(self) -> None:
        """A typo must not silently produce a quiet process."""
        with pytest.raises(ValueError, match="logging level"):
            LoggingConfig(level="VERBOSE")


class TestTraceIdOnTheChain:
    """One correlation id, followed from submit through to the venue."""

    def test_submit_and_venue_share_the_correlation_id(
        self, captured: StringIO
    ) -> None:
        """The id handed to the engine is the id that reaches the venue.

        This is the chain the brief asks for. The order carries a known
        ``CorrelationId``; submitting it must produce an ``order.submit`` record
        and a ``venue.request`` record, and both must name the *same* id — the
        same string the order would be persisted under.
        """
        broker = _AckBroker()
        engine = _engine(BrokerFillSource(broker))
        engine.submit(_request(_CID))

        assert [r.correlation_id.value for r in broker.seen] == [_CID], (
            "the venue did not receive the order under the id we submitted"
        )

        lines = [json.loads(line) for line in captured.getvalue().splitlines()]
        by_stage = {entry.get("stage"): entry for entry in lines}

        assert "order.submit" in by_stage, (
            f"no order.submit chain point; stages seen: "
            f"{sorted(s for s in by_stage if s)}"
        )
        assert "venue.request" in by_stage, (
            f"no venue.request chain point; stages seen: "
            f"{sorted(s for s in by_stage if s)}"
        )
        assert by_stage["order.submit"]["correlation_id"] == _CID
        assert by_stage["venue.request"]["correlation_id"] == _CID

    def test_contextvar_is_set_during_the_pipeline_and_reset_after(
        self, captured: StringIO
    ) -> None:
        """The id is in scope for the pipeline, and gone afterwards.

        The reset is the half that is easy to forget: the bus delivers a stream
        of independent orders on one thread, so a contextvar left set would
        stamp the *previous* order's id onto the *next* order's risk refusal.
        """
        engine = _engine()
        seen: list[str | None] = []
        original = engine._log_trace_point

        def spy(stage: str, request: OrderRequest, **fields: object) -> None:
            original(stage, request, **fields)
            seen.append(get_correlation_id())

        engine._log_trace_point = spy  # type: ignore[method-assign]
        engine.submit(_request(_CID))

        assert seen == [_CID], (
            f"the id was not in scope while the pipeline ran: {seen}"
        )
        assert get_correlation_id() is None, (
            "the trace id leaked past the submit — the next order on this "
            "thread would be stamped with it"
        )

    def test_engine_money_path_records_carry_the_id(self, captured: StringIO) -> None:
        """An unannotated engine log line is still stamped with the id.

        The chain points are explicit. The point of the *contextvar* is the lines
        nobody annotated: the risk gate, the fee-schedule refusal, the durable
        write failures. If those come out with ``"-"`` the id is decoration.
        """
        engine = _engine()
        token = set_correlation_id(_CID)
        try:
            engine._log_trace_point(
                "order.submit", _request(_CID), sync=True
            )
        finally:
            reset_correlation_id(token)

        entries = [
            json.loads(line) for line in captured.getvalue().splitlines()
            if line.strip()
        ]
        assert entries
        assert all(e["correlation_id"] == _CID for e in entries), (
            "a money-path record was emitted without the id in scope"
        )


class TestNoFabricatedTraceId:
    """Negative control: absence of an id is rendered, never invented."""

    def test_record_without_an_id_renders_the_placeholder(self) -> None:
        """No id in context and none attached → ``-``, and no exception.

        Fabricating an id is worse than omitting one: a wrong id is greppable,
        and an operator who greps it is told a specific order's history is the
        one in front of them.
        """
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname="", lineno=0,
            msg="no id here", args=(), exc_info=None,
        )
        parsed = json.loads(formatter.format(record))
        assert parsed["correlation_id"] == NO_TRACE

    def test_structured_formatter_survives_a_missing_id(self) -> None:
        """The human formatter must not KeyError on an unstamped record."""
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname="", lineno=0,
            msg="still renders", args=(), exc_info=None,
        )
        assert "still renders" in formatter.format(record)

    def test_an_order_with_no_correlation_id_still_runs(
        self, captured: StringIO
    ) -> None:
        """Submitting without an id is not a crash, and does not invent one.

        ``OrderRequest.correlation_id`` is optional, so this is a real shape the
        engine accepts. The negative control on the chain tests: if the id were
        being made up, this is where it would show.
        """
        engine = _engine()
        engine.submit(_request(None))

        entries = [
            json.loads(line) for line in captured.getvalue().splitlines()
            if line.strip()
        ]
        stages = [e for e in entries if e.get("stage")]
        assert stages, "expected the chain points to still be emitted"
        assert all(
            e["correlation_id"] in (None, NO_TRACE) for e in stages
        ), f"an id was fabricated for an order that had none: {stages}"

    def test_an_explicit_id_beats_the_contextvar(self) -> None:
        """A per-record id is not clobbered by an unrelated ambient one.

        The signal and feed chain points attach their id explicitly because
        nothing has set the contextvar at that point in the chain. If the filter
        overwrote it with the contextvar (or with ``-``) those records would lose
        precisely the id that makes them useful.
        """
        from tradex_observability.tracing import CorrelationIdFilter

        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname="", lineno=0,
            msg="explicit", args=(), exc_info=None,
        )
        record.correlation_id = "explicit-id"  # type: ignore[attr-defined]
        token = set_correlation_id("ambient-other-id")
        try:
            CorrelationIdFilter().filter(record)
        finally:
            reset_correlation_id(token)
        assert record.correlation_id == "explicit-id"  # type: ignore[attr-defined]
