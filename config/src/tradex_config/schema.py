"""Application configuration schema.

Defines the configuration structure for the v4 trading platform.
Frozen dataclass tree with strict shape validation.
"""

from __future__ import annotations

import os
import typing
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from tradex_domain import BrokerId
from tradex_domain.constants import (
    BOOK_MATCHING,
    DEFAULT_BACKTEST_RISK_FRACTION,
    PAPER_DEFAULT,
    SLIPPAGE_MODEL,
)
from tradex_domain.instruments import Equity, Instrument
from tradex_domain.paths import default_runtime_dir
from tradex_domain.risk_budget import (
    REQUIRED_RISK_LIMITS,
    UNBOUNDED,
    RiskBudget,
    is_unbounded,
)

#: The tape a live session streams, named by an operator. Never defaulted to an
#: index or a universe: the platform is not asked to guess what to trade.
FEED_INSTRUMENTS_ENV = "TRADEX_FEED_INSTRUMENTS"
#: Names the credential file an operator explicitly loaded, so boot can check
#: its permissions. Set by the entry points that call ``load_env_file``; there is
#: no default, because a session that loads no credential file has nothing to
#: check (AC-17, lane 5d).
ENV_FILE_ENV_VAR = "TRADEX_ENV_FILE"

#: The one text form a tape entry may take. The exchange is part of the name
#: because a trading symbol alone is not a security — ``RELIANCE`` is an NSE
#: cash equity and a BSE one — and this repository has already paid for
#: collapsing two securities onto one symbol.
_INSTRUMENT_ENTRY_FORM = "EXCHANGE:SYMBOL (e.g. NSE:RELIANCE)"


def parse_feed_instruments(raw: str | Iterable[object]) -> tuple[Instrument, ...]:
    """Parse named tape entries into a de-duplicated, ordered instrument tuple.

    Accepts the ``$TRADEX_FEED_INSTRUMENTS`` string (comma separated) or, for
    the dict/JSON path, a sequence of the same entries. Surrounding whitespace
    and empty slots are dropped — a trailing comma is a typo, not a request to
    trade nothing — and duplicates are proven once, first occurrence winning, so
    the order the operator wrote is the order the boot reconciles.

    A malformed entry raises :class:`ValueError` naming both the environment
    variable and the offending entry. Dropping it and starting with whatever
    survived is the one behaviour this must never have: the session would come
    up trading a tape the operator did not write, with the typo discarded in
    silence.
    """
    entries = raw.split(",") if isinstance(raw, str) else raw
    ordered: dict[Any, Instrument] = {}
    for entry in entries:
        if isinstance(entry, str) and not entry.strip():
            # An unset variable, a trailing comma and a doubled separator all
            # land here: nothing was named, which is legal and means no tape.
            continue
        if not isinstance(entry, str):
            raise ValueError(
                f"{FEED_INSTRUMENTS_ENV} entries must be strings of the form "
                f"{_INSTRUMENT_ENTRY_FORM}; got {entry!r}"
            )
        parts = entry.split(":")
        if len(parts) != 2 or not all(part.strip() for part in parts):
            raise ValueError(
                f"{FEED_INSTRUMENTS_ENV} entry {entry!r} is not "
                f"{_INSTRUMENT_ENTRY_FORM}"
            )
        exchange, symbol = (part.strip() for part in parts)
        try:
            instrument = Equity.of(exchange, symbol)
        except ValueError as exc:
            raise ValueError(
                f"{FEED_INSTRUMENTS_ENV} entry {entry!r} is not a tradable "
                f"instrument: {exc}"
            ) from exc
        ordered.setdefault(instrument.instrument_id, instrument)
    return tuple(ordered.values())


def default_feed_instruments() -> tuple[Instrument, ...]:
    """Return the tape named by ``$TRADEX_FEED_INSTRUMENTS``, empty if unset.

    Resolved on every call, not captured at import time, for the same reason
    ``default_runtime_dir_str`` is: a module constant would freeze the first
    value this process ever saw and go stale for anything that sets the
    variable later. Being the field's default factory is also what makes the
    variable reach callers that build their own ``AppConfig`` — ``boot()`` reads
    the config object, never the environment.
    """
    return parse_feed_instruments(os.environ.get(FEED_INSTRUMENTS_ENV, ""))


def default_runtime_dir_str() -> str:
    """Return the shared runtime root as a string.

    Resolved on every call, not captured at import time, so the config layer
    observes the same ``$TRADEX_RUNTIME_DIR`` the broker layer does. The
    fallback used to be the bare relative ``".tradex_v4"``, which was
    cwd-relative *and* a different directory from the ``<repo>/runtime`` the
    broker layer anchored to, so the two layers forked token state, TOTP
    cooldowns and instrument caches whenever the variable was unset.
    """
    return str(default_runtime_dir())


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Risk management configuration."""

    max_order_value: Decimal | None = None
    max_position_value: Decimal | None = None
    max_orders_per_minute: int | None = None
    kill_switch_default: bool = False
    reject_unknown_market_value: bool = False
    max_daily_loss_amt: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    #: Live opening exposure requires a quote-derived mark no older than this
    #: many seconds. The live composition root enables this fail-closed gate.
    require_fresh_marks: bool = False
    max_mark_age_seconds: float = 5.0
    #: Optional zero-arg callable returning available cash (Decimal).
    #: When set, ``boot()`` binds it to the engine's ``RiskManager`` so
    #: every BUY in :meth:`RiskManager.check` is rejected if its incoming
    #: notional exceeds the returned cash. When None, the cash gate is
    #: skipped (backward-compat with risk configs that do not track cash).
    cash_provider: Callable[[], Decimal | None] | None = None
    #: AC-9: per-strategy risk budgets, keyed by strategy id. ``boot()`` passes
    #: this to ``RiskManager``, which resolves a request's budget by the
    #: strategy identifier rather than by the ``name@version`` text in ``tag``.
    #: A strategy with no entry here falls back to the global caps above.
    budgets: dict[str, RiskBudget] = field(default_factory=dict)
    #: F-40: the share of account equity one trade may lose, used to size an
    #: order. Defaults to the backtest's own default so paper/live and backtest
    #: start from the same policy; ``boot()`` builds the single
    #: ``account_sizer`` from this, the notional ceiling, and the live balance.
    risk_fraction: Decimal | None = Decimal(str(DEFAULT_BACKTEST_RISK_FRACTION))
    #: Short-selling policy. False (the platform default) denies any
    #: SELL that would open or grow a short position; a SELL that only
    #: reduces a long stays allowed. ``boot()`` passes this to
    #: ``RiskManager``. Operators who trade short-enabled books set
    #: ``risk.allow_short: true``.
    allow_short: bool = False

    def __post_init__(self) -> None:
        if self.max_order_value is not None and not isinstance(self.max_order_value, Decimal):
            object.__setattr__(
                self,
                "max_order_value",
                Decimal(str(self.max_order_value)),
            )
        if self.max_position_value is not None and not isinstance(self.max_position_value, Decimal):
            object.__setattr__(
                self,
                "max_position_value",
                Decimal(str(self.max_position_value)),
            )
        if self.max_daily_loss_amt is not None and not isinstance(self.max_daily_loss_amt, Decimal):
            object.__setattr__(
                self,
                "max_daily_loss_amt",
                Decimal(str(self.max_daily_loss_amt)),
            )
        if self.max_drawdown_pct is not None and not isinstance(self.max_drawdown_pct, Decimal):
            object.__setattr__(
                self,
                "max_drawdown_pct",
                Decimal(str(self.max_drawdown_pct)),
            )
        if self.max_mark_age_seconds < 0:
            raise ValueError("max_mark_age_seconds must be non-negative")
        # Strict parsing of budgets: the dict/JSON path may hand us plain dicts
        # (from ``from_dict``) rather than RiskBudget objects. Convert here so
        # every entry is a real RiskBudget and Decimal-coerced, exactly like the
        # scalar limits above. A caller who built the dataclass directly with
        # RiskBudget objects passes through unchanged.
        normalized: dict[str, RiskBudget] = {}
        for sid, budget in (self.budgets or {}).items():
            if isinstance(budget, RiskBudget):
                normalized[sid] = budget
            elif isinstance(budget, dict):
                payload = dict(budget)
                payload.setdefault("strategy_id", sid)
                normalized[sid] = RiskBudget(**payload)
            else:
                raise ValueError(
                    f"risk budget for {sid!r} must be a RiskBudget or an "
                    f"object, got {type(budget).__name__}"
                )
        object.__setattr__(self, "budgets", normalized)

    def missing_limits(self) -> tuple[str, ...]:
        """The required limits that are *unset* — not opted out, just absent.

        AC-10: no risk limit is optional in paper or live, so a limit left at
        ``None`` is a missing limit and boot refuses. The explicit
        ``UNBOUNDED`` sentinel is the documented way to say "run this one
        unlimited on purpose" and is deliberately NOT reported here — that is
        the whole difference between a choice and an omission.
        """
        return tuple(
            name
            for name in REQUIRED_RISK_LIMITS
            if getattr(self, name) is None
        )

    def effective_limits(self) -> dict[str, object]:
        """The required limits as ``RiskManager`` wants them.

        ``UNBOUNDED`` is an *operator-facing* marker meaning "no limit". It is
        not a number the risk gate can use: handed to ``RiskManager`` as-is,
        ``max_order_value = -1`` would deny every order (every notional exceeds
        -1) and ``max_daily_loss_amt = -1`` would trip the loss guard on any
        gain. The opt-out has to become the gate's own "no limit" sentinel
        (``None``) here, at the one boundary where the two vocabularies meet.

        ``None`` never reaches here — ``require_limits()`` has already refused
        the boot by then — so this cannot be the path that turns a missing
        limit into an unbounded one.
        """
        caps: dict[str, object] = {}
        for name in REQUIRED_RISK_LIMITS:
            value = getattr(self, name)
            caps[name] = None if is_unbounded(value) else value
        return caps

    def require_limits(self) -> None:
        """Raise unless every required limit is set (or explicitly UNBOUNDED).

        Called by ``boot()`` for paper and live. Backtest and replay do not
        call it: they trade recorded data with no venue to be stopped at, and
        the promotion bar is a separate concern (see the spec's AC-10 scope).
        """
        missing = self.missing_limits()
        if not missing:
            return
        raise MissingRiskLimitError(missing)


class MissingRiskLimitError(ValueError):
    """A required risk limit was left unset at boot (AC-10).

    Carries the missing names so an operator sees every gap at once, not one
    per restart. The remedy is either to name a value for each, or to set the
    limit to ``UNBOUNDED`` — the documented, explicit opt-out that keeps
    "unlimited" a decision rather than an accident of a default.
    """

    def __init__(self, missing: Iterable[str]) -> None:
        self.missing = tuple(missing)
        named = ", ".join(self.missing)
        super().__init__(
            f"refusing to boot: risk limit(s) unset: {named}. "
            f"No risk limit is optional in paper or live. Name a value for "
            f"each, or set it to the explicit unbounded opt-out "
            f"tradex_domain.UNBOUNDED ({UNBOUNDED}) to say so deliberately."
        )


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    """Broker connection configuration."""

    name: str = "paper"
    environment: str = PAPER_DEFAULT


@dataclass(frozen=True, slots=True)
class PersistenceConfig:
    """Optional local SQLite durability for orders and idempotency results.

    ``path`` is deliberately opt-in: the default runtime remains in-memory and
    creates no files. When set, both stores share this SQLite database.
    """

    path: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Execution-cost and strategy-bridge configuration, shared across modes.

    These settings make fees, slippage, and fill timing **consistent** between
    backtest and the reactive paper/live path (parity review HIGH-6b) — the
    same models are wired into the fill sources and the execution engine by
    ``runtime.startup.boot``. Costs are deducted by default; set False to
    reproduce the historical zero-cost, next-bar-open behavior.
    """

    fees_enabled: bool = True
    slippage_bps: Decimal | None = None
    fill_reference: str = "next_open"
    #: Which paper fill path this run uses (AC-14, spec lane 2d).
    #:
    #: ``"slippage_model"`` (the default) fills at the live quote through a
    #: slippage model — the path paper mode has always used, kept as the default
    #: so a run that names no path behaves exactly as it did before. The other
    #: value, ``"book_matching"``, fills by walking the paper broker's simulated
    #: multi-level book and is the ORACLE the slippage path is measured against
    #: (``trading/tests/parity/test_slippage_vs_book_parity.py``). The two
    #: coexist on purpose: running the same recorded session through both is how
    #: the paper path is proven to agree with itself before a strategy is
    #: promoted, so neither can be removed when the other is fixed.
    fill_path: str = SLIPPAGE_MODEL
    #: Half-spread the oracle's book is seeded with, in basis points of the mid.
    #: A top-of-book fill costs ``mid * book_spread_bps / 10_000`` per share,
    #: which is exactly what ``slippage_bps`` charges through the production
    #: ``PercentageSlippageModel`` — so the two paths are comparable at the same
    #: constant even when the price moves (see
    #: ``trading/tests/parity/test_slippage_vs_book_parity.py``).
    book_spread_bps: Decimal = Decimal("5")
    #: Register the auto-discovered example strategies on boot.
    #:
    #: Off by default. The discovered examples are bound to a hardcoded
    #: instrument (``sma_cross_example`` trades RELIANCE) and place real orders,
    #: so wiring them into a live account would trade something nobody deployed.
    #: They remain discoverable so the chart/backtest route can offer them to a
    #: user who asks for one by name.
    auto_register_examples: bool = False
    #: F-45: how many bars a deferred ``next_open`` order may sit in the queue
    #: before the engine cancels it. The engine's own default is ``0``, which
    #: *disables* expiry, so leaving this unset made the documented max-age
    #: sweep a silent no-op in production. Non-zero by default: a deferred
    #: order that never fills is a position the operator did not intend, and
    #: ``0`` is reachable explicitly. Must be >= 0.
    pending_max_age_bars: int = 3

    def __post_init__(self) -> None:
        if self.slippage_bps is not None and not isinstance(
            self.slippage_bps, Decimal
        ):
            object.__setattr__(
                self,
                "slippage_bps",
                Decimal(str(self.slippage_bps)),
            )
        if self.fill_reference not in ("next_open", "signal_close"):
            raise ValueError(
                f"fill_reference must be 'next_open' or 'signal_close', "
                f"got {self.fill_reference!r}"
            )
        if self.fill_path not in (BOOK_MATCHING, SLIPPAGE_MODEL):
            raise ValueError(
                f"fill_path must be '{BOOK_MATCHING}' or '{SLIPPAGE_MODEL}', "
                f"got {self.fill_path!r}"
            )
        if not isinstance(self.book_spread_bps, Decimal):
            object.__setattr__(
                self, "book_spread_bps", Decimal(str(self.book_spread_bps))
            )
        if self.book_spread_bps < 0:
            raise ValueError(
                f"book_spread_bps must be >= 0, got {self.book_spread_bps!r}"
            )
        if self.pending_max_age_bars < 0:
            raise ValueError(
                f"pending_max_age_bars must be >= 0, got {self.pending_max_age_bars!r}"
            )


@dataclass(frozen=True, slots=True)
class FeedConfig:
    """Feed continuity configuration, read by ``boot`` (F-43).

    ``max_missing_bars``
        How many bars of hole feed recovery tolerates between the last accepted
        bar and the replayed series after a socket reconnect. Over the bound,
        recovery fails closed and the feed goes ``HALTED`` rather than trading a
        discontinuous book. Non-zero by default so the contract holds without
        configuration; the default sits above a venue's structural tail hole
        (Dhan's NSE 1m series stops at 15:14). Must be >= 0.
    """

    max_missing_bars: int = 20

    def __post_init__(self) -> None:
        if self.max_missing_bars < 0:
            raise ValueError(
                f"max_missing_bars must be >= 0, got {self.max_missing_bars!r}"
            )


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Structured-logging configuration, read by ``boot`` (AC-17 / lane 5c).

    The structured logging seam in ``tradex_runtime.logging_config`` existed
    but had no production call site, so a real run emitted plain stdlib lines
    and the correlation id was a contextvar nobody ever set. These two fields
    are what make it operator-tunable rather than hardcoded:

    ``level``
        Root logger level, by name (``DEBUG``/``INFO``/``WARNING``/``ERROR``/
        ``CRITICAL``, case-insensitive). A bad name raises rather than silently
        falling back, because a typo that quietly disabled ERROR output on a
        trading process is not a default anyone wants.
    ``json_format``
        JSON lines for ELK/Datadog instead of the human-readable format.
    """

    level: str = "INFO"
    json_format: bool = False

    #: Level names an operator may name. Kept here so the accepted set is one
    #: list rather than a branch in ``__post_init__`` and a lookup in boot.
    _LEVELS: typing.ClassVar[frozenset[str]] = frozenset(
        {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    )

    def __post_init__(self) -> None:
        level = str(self.level).strip().upper()
        if level not in self._LEVELS:
            raise ValueError(
                f"logging level must be one of {sorted(self._LEVELS)}, "
                f"got {self.level!r}"
            )
        object.__setattr__(self, "level", level)

    def level_value(self) -> int:
        """The ``logging`` module constant for :attr:`level`.

        Resolved through ``getattr`` on the ``logging`` module so this stays
        stdlib-only and there is no second level table to keep in sync.
        """
        import logging

        return int(getattr(logging, self.level))


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Application configuration.

    Attributes
    ----------
    broker_id : BrokerId
        The broker to use (default: PAPER).
    mode : str
        Execution mode: "paper", "backtest", "replay", "live".
    risk : RiskConfig
        Risk management configuration.
    runtime_dir : str
        Directory for runtime data (logs, cache, etc.). Defaults to
        ``$TRADEX_RUNTIME_DIR`` or the repo-anchored ``<repo>/runtime``,
        shared with the broker layer (see ``default_runtime_dir_str``).
    kill_switch_default : bool
        Default state of the kill switch.
    live_enabled : bool
        Whether live trading is explicitly enabled.
    environment : str
        Runtime environment (PAPER, SANDBOX, LIVE).
    broker : BrokerConfig
        Broker connection configuration.
    persistence : PersistenceConfig
        Persistence configuration.
    logging : LoggingConfig
        Structured-logging level and output format, named by
        ``$TRADEX_LOG_LEVEL`` and ``$TRADEX_LOG_JSON`` and applied by
        ``boot()`` (AC-17 / lane 5c).
    feed : FeedConfig
        Feed continuity bounds, applied by ``boot()`` when it builds the
        recovery coordinator (F-43).
    feed_instruments : tuple[Instrument, ...]
        The tape a live session must be able to prove before it starts (AC-20).
        Empty by default — paper, backtest and replay need no tape — and named
        by ``$TRADEX_FEED_INSTRUMENTS`` (see :func:`parse_feed_instruments`).
        ``boot()`` reads this field when it is given no ``feed_instruments``.
    """

    broker_id: BrokerId = BrokerId.PAPER
    mode: str = "paper"
    risk: RiskConfig = field(default_factory=RiskConfig)
    runtime_dir: str = field(default_factory=default_runtime_dir_str)
    kill_switch_default: bool = False
    live_enabled: bool = False
    live_orders_enabled: bool = False
    environment: str = PAPER_DEFAULT
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    persistence: PersistenceConfig = field(default_factory=PersistenceConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    feed: FeedConfig = field(default_factory=FeedConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    feed_instruments: tuple[Instrument, ...] = field(
        default_factory=default_feed_instruments
    )

    def secret_files(self) -> tuple[Path, ...]:
        """Credential files this session's secrets live in, for a mode check.

        Two sources: the env file named by ``TRADEX_ENV_FILE`` (the operator's
        opt-in credential file, the one a human actually creates by hand), and
        the runtime dir that holds the broker's durable token files. The token
        layer creates its own files privately; an operator-created
        ``.env.local`` does not arrive that way, so boot checks both (AC-17,
        lane 5d).

        Paths that do not exist are returned anyway: "no credentials on disk"
        is a legitimate configuration, and the permission check treats absence
        as a pass rather than guessing which files an operator meant.
        """
        candidates: list[Path] = []
        env_file = os.environ.get(ENV_FILE_ENV_VAR)
        if env_file:
            candidates.append(Path(env_file))
        # ``runtime_dir`` holds the token files minted by the broker auth layer.
        candidates.append(Path(self.runtime_dir) / "tokens")
        return tuple(candidates)

    def __post_init__(self) -> None:
        # The tape is a set, not a list: one instrument is proven once however
        # many times it was named. Held here rather than only in the parser so
        # the invariant survives every way in — the env var, ``from_dict``, or a
        # hand-built config.
        tape = self.feed_instruments
        # A bare string is iterable, and iterating it would report the letter
        # ``N`` instead of the value the caller passed.
        entries = [tape] if isinstance(tape, str) else tape
        ordered: dict[Any, Instrument] = {}
        for instrument in entries:
            if not isinstance(instrument, Instrument):
                raise ValueError(
                    f"{FEED_INSTRUMENTS_ENV} names instruments, not text: pass "
                    f"Equity.of(exchange, symbol) objects, or the "
                    f"{_INSTRUMENT_ENTRY_FORM} string form through "
                    f"parse_feed_instruments(); got {instrument!r}"
                )
            ordered.setdefault(instrument.instrument_id, instrument)
        object.__setattr__(self, "feed_instruments", tuple(ordered.values()))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        """Build an AppConfig from a plain dictionary with strict validation."""
        allowed = {
            "broker_id",
            "mode",
            "risk",
            "runtime_dir",
            "kill_switch_default",
            "live_enabled",
            "live_orders_enabled",
            "environment",
            "broker",
            "persistence",
            "execution",
            "logging",
            "feed_instruments",
        }
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown config sections: {sorted(unknown)}")

        broker_id_str = str(data.get("broker_id", PAPER_DEFAULT))
        valid_ids = [b.value for b in BrokerId]
        try:
            broker_id = BrokerId(broker_id_str)
        except ValueError:
            raise ValueError(
                f"invalid broker_id {broker_id_str!r}; "
                f"valid values: {valid_ids}"
            ) from None

        broker = _build(BrokerConfig, data.get("broker"))
        risk = _build(RiskConfig, data.get("risk"))
        persistence = _build(PersistenceConfig, data.get("persistence"))
        execution = _build(ExecutionConfig, data.get("execution"))
        logging = _build(LoggingConfig, data.get("logging"))
        # Absent from the dict means "unset", which is the environment's
        # default — the same reading ``AppConfig()`` gets, so the two paths
        # cannot disagree about what the operator named.
        feed_instruments = (
            parse_feed_instruments(data["feed_instruments"])
            if "feed_instruments" in data
            else default_feed_instruments()
        )

        return cls(
            broker_id=broker_id,
            mode=str(data.get("mode", "paper")),
            risk=risk,
            runtime_dir=str(data.get("runtime_dir") or default_runtime_dir_str()),
            kill_switch_default=bool(data.get("kill_switch_default", False)),
            live_enabled=bool(data.get("live_enabled", False)),
            live_orders_enabled=bool(data.get("live_orders_enabled", False)),
            environment=str(data.get("environment", PAPER_DEFAULT)),
            broker=broker,
            persistence=persistence,
            execution=execution,
            logging=logging,
            feed_instruments=feed_instruments,
        )


def _build(cls: type, data: object) -> Any:
    """Construct a frozen dataclass from an optional dict with strict keys."""
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ValueError(f"{cls.__name__} config must be an object")
    allowed = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys: {sorted(unknown)}")
    return cls(**data)


__all__ = [
    "FEED_INSTRUMENTS_ENV",
    "AppConfig",
    "BrokerConfig",
    "ExecutionConfig",
    "LoggingConfig",
    "MissingRiskLimitError",
    "PersistenceConfig",
    "RiskConfig",
    "_build",
    "default_feed_instruments",
    "default_runtime_dir_str",
    "parse_feed_instruments",
]
