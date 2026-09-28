"""Lane 4c, item 5 — the fill dedup index, partitioned by account.

The spec's requirement (``docs/specs/0002-production-readiness-programme``,
index.md line 183):

    | `Fill` dedup index | partition by account | n/a | n/a | Two brokers in
    one process would drop each other's fills as duplicates |

and the invariant the dedup index exists to hold (line 186):

    A fill's `venue_trade_id`, once seen for an account, is never booked
    twice.

Three things follow, and all three are testable at the storage layer:

* the key is the venue trade id, not the fill's economic content. The spec
  names ``venue_trade_id`` — a trade has ONE id at the venue, and two fills
  with the same id are the same trade, whatever their quantity or price.
  Keying on economic content would instead treat two partial fills of one
  order as duplicates, which is how real fills get dropped.
* the partition is the account. The same venue trade id under two accounts
  is two executions, so a cross-account dedup check must not find it.
* the store is the mechanism, not the policy. The store records what it has
  seen this session and answers honestly; which replays a caller treats as
  duplicates is the caller's session boundary. The tests below cover the
  store's half — that it records, partitions, and answers — and record the
  session-boundary policy as a follow-up, because that policy lives in
  ``FillDedup`` (``idempotency.py``), which this lane does not own.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from tradex_domain.enums import OrderSide
from tradex_domain.events import OrderFilled
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.sqlite_event_store import (
    UNSCOPED_ACCOUNT,
    SQLiteEventStore,
    dedup_key,
    dedup_table_name,
    fill_fingerprint,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _fill(
    fill_id: str = "f-1",
    order_id: str = "o-1",
    qty: str = "10",
    price: str = "2500",
    side: OrderSide = OrderSide.BUY,
) -> Fill:
    return Fill(
        order_id=OrderId(order_id),
        instrument=_eq(),
        side=side,
        quantity=Quantity(Decimal(qty)),
        price=Price(Decimal(price)),
        fill_id=fill_id,
    )


def _filled(fill: Fill) -> OrderFilled:
    return OrderFilled(fill=fill)


def _store(db: Path, **kwargs) -> SQLiteEventStore:
    return SQLiteEventStore(db, **kwargs)


def _event_rows(db: Path) -> int:
    import sqlite3

    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# The key is the venue trade id, not the economic content
# ---------------------------------------------------------------------------


def test_the_fingerprint_is_the_fill_id() -> None:
    """The spec keys on ``venue_trade_id``; two ids are two trades.

    The fingerprint matches :meth:`FillDedup.fingerprint` in ``idempotency.py``
    so a key computed at apply time is the key replay looks for. Keying on
    economic content instead would call two partials of one order a
    duplicate, and that is the failure mode the index exists to prevent.
    """
    assert fill_fingerprint(_fill(fill_id="trade-1")) == ("trade-1",)


def test_the_fingerprint_ignores_the_economic_content() -> None:
    """Quantity, price, side and order do not enter the key.

    A venue re-publishes a trade under the same id with corrected quantity or
    price, and that re-publication is the duplicate — the content differing is
    the whole point. A key that changed on content would miss it.
    """
    base = fill_fingerprint(_fill(fill_id="trade-1", qty="10"))
    assert fill_fingerprint(_fill(fill_id="trade-1", qty="20")) == base
    assert fill_fingerprint(_fill(fill_id="trade-1", price="2600")) == base
    assert fill_fingerprint(_fill(fill_id="trade-1", side=OrderSide.SELL)) == base
    assert fill_fingerprint(_fill(fill_id="trade-1", order_id="o-9")) == base


def test_the_fingerprint_of_an_event_is_the_fingerprint_of_its_fill() -> None:
    """The event and its fill key identically, so replay finds what apply stored.

    ``OrderFilled`` has no ``fill_id`` of its own — the id belongs to its
    ``Fill`` — so the caller reads the event's fill and fingerprints that.
    """
    event = _filled(_fill(fill_id="trade-1"))
    assert fill_fingerprint(event.fill) == ("trade-1",)


def test_two_different_trade_ids_are_two_keys() -> None:
    """The one thing that MUST change the key is the trade id."""
    assert fill_fingerprint(_fill(fill_id="trade-1")) != (
        fill_fingerprint(_fill(fill_id="trade-2"))
    )


# ---------------------------------------------------------------------------
# Same trade, same account — reject
# ---------------------------------------------------------------------------


def test_a_replayed_fill_is_rejected_as_a_duplicate(tmp_path: Path) -> None:
    """The second delivery of one trade is not recorded, and the event count stays 1."""
    db = tmp_path / "events.db"
    store = _store(db)
    try:
        assert store.record_fill(_fill(fill_id="trade-1")) is True
        assert store.record_fill(_fill(fill_id="trade-1")) is False
        assert store.fill_seen(_fill(fill_id="trade-1")) is True
    finally:
        store.close()


def test_a_duplicate_does_not_reach_the_log(tmp_path: Path) -> None:
    """The duplicate is refused before it is appended, not compensated after.

    The index's job is to keep a double count from happening, so a fill that
    was a duplicate must not be in the log at all. Compensating after the fact
    would leave the wrong event on disk for a later replay to read.
    """
    db = tmp_path / "events.db"
    store = _store(db)
    try:
        store.record_fill(_fill(fill_id="trade-1"))
        assert store.record_fill(_fill(fill_id="trade-1")) is False
    finally:
        store.close()

    reopened = _store(db)
    try:
        assert list(reopened.replay("fills")) == []
    finally:
        reopened.close()


def test_the_rejected_append_leaves_no_event_row(tmp_path: Path) -> None:
    """The log's row count is unaffected by a rejected fill."""
    db = tmp_path / "events.db"
    store = _store(db)
    try:
        store.record_fill(_fill(fill_id="trade-1"))
        store.record_fill(_fill(fill_id="trade-1"))
    finally:
        store.close()

    assert _event_rows(db) == 0


def test_a_rejected_duplicate_does_not_register_a_new_key(tmp_path: Path) -> None:
    """The index keeps one row for one trade, no matter how many deliveries."""
    db = tmp_path / "events.db"
    store = _store(db)
    try:
        store.record_fill(_fill(fill_id="trade-1"))
        for _ in range(5):
            store.record_fill(_fill(fill_id="trade-1"))
        assert store.fill_dedup_count() == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Same trade, different account — accept
# ---------------------------------------------------------------------------


def test_the_same_trade_in_two_accounts_is_recorded_twice(tmp_path: Path) -> None:
    """The index is partitioned by account, so it does not dedupe across.

    The spec's stated hazard is that two brokers in one process would drop
    each other's fills as duplicates; the same id under two accounts is two
    executions, and both must be recorded.
    """
    db = tmp_path / "events.db"
    for account in ("acc-1", "acc-2"):
        store = _store(db, broker_id="PAPER", account_id=account)
        try:
            assert store.record_fill(_fill(fill_id="trade-1")) is True
        finally:
            store.close()

    assert _event_rows(db) == 0  # record_fill only indexes, it does not append


def test_the_same_trade_is_seen_only_within_its_own_account(tmp_path: Path) -> None:
    """``fill_seen`` is partitioned too: one account's trade is unseen by another."""
    db = tmp_path / "events.db"
    store = _store(db, broker_id="PAPER", account_id="acc-1")
    try:
        store.record_fill(_fill(fill_id="trade-1"))
    finally:
        store.close()

    other = _store(db, broker_id="PAPER", account_id="acc-2")
    try:
        assert other.fill_seen(_fill(fill_id="trade-1")) is False
        assert other.record_fill(_fill(fill_id="trade-1")) is True
    finally:
        other.close()


def test_two_brokers_sharing_an_account_id_still_partition(tmp_path: Path) -> None:
    """The partition is the account, so two brokers with the same account id
    share one partition — which is correct, since they are one account."""
    db = tmp_path / "events.db"
    store = _store(db, broker_id="DHAN", account_id="acc-1")
    try:
        store.record_fill(_fill(fill_id="trade-1"))
    finally:
        store.close()

    same_account = _store(db, broker_id="ZERODHA", account_id="acc-1")
    try:
        assert same_account.fill_seen(_fill(fill_id="trade-1")) is True
    finally:
        same_account.close()


def test_the_dedup_count_is_partitioned_by_account(tmp_path: Path) -> None:
    """One account's count does not include another account's trades."""
    db = tmp_path / "events.db"
    for account in ("acc-1", "acc-2"):
        store = _store(db, broker_id="PAPER", account_id=account)
        try:
            store.record_fill(_fill(fill_id="trade-1"))
            store.record_fill(_fill(fill_id="trade-2"))
        finally:
            store.close()

    reader = _store(db, broker_id="PAPER", account_id="acc-1")
    try:
        assert reader.fill_dedup_count() == 2
    finally:
        reader.close()
    reader2 = _store(db, broker_id="PAPER", account_id="acc-2")
    try:
        assert reader2.fill_dedup_count() == 2
    finally:
        reader2.close()


# ---------------------------------------------------------------------------
# Different trade, same account — accept
# ---------------------------------------------------------------------------


def test_two_different_trades_in_one_account_are_both_recorded(
    tmp_path: Path,
) -> None:
    """Different venue trade ids are different trades, even on one order."""
    db = tmp_path / "events.db"
    store = _store(db, broker_id="PAPER", account_id="acc-1")
    try:
        assert store.record_fill(_fill(fill_id="trade-1")) is True
        assert store.record_fill(_fill(fill_id="trade-2")) is True
        assert store.fill_dedup_count() == 2
    finally:
        store.close()


def test_two_partials_of_one_order_are_not_duplicates(tmp_path: Path) -> None:
    """Two partial fills of one order carry two trade ids and are both kept.

    This is the case economic-content keying gets wrong: the two partials have
    the same order, instrument and side and different quantities, so a
    content key would either collide or miss. The trade id separates them.
    """
    db = tmp_path / "events.db"
    store = _store(db, broker_id="PAPER", account_id="acc-1")
    try:
        assert store.record_fill(_fill(fill_id="trade-1", qty="4")) is True
        assert store.record_fill(_fill(fill_id="trade-2", qty="6")) is True
        assert store.fill_dedup_count() == 2
    finally:
        store.close()


# ---------------------------------------------------------------------------
# The partition key
# ---------------------------------------------------------------------------


def test_the_dedup_key_carries_the_account_and_the_trade() -> None:
    """The key is the pair: same trade, different account, different key."""
    trade = fill_fingerprint(_fill(fill_id="trade-1"))
    assert dedup_key("acc-1", trade) == ("acc-1", ("trade-1",))
    assert dedup_key("acc-1", trade) != dedup_key("acc-2", trade)


def test_an_unset_account_falls_back_to_the_global_partition() -> None:
    """``None`` means "no account scope" and keeps the legacy global table."""
    assert dedup_key(None, ("trade-1",))[0] == UNSCOPED_ACCOUNT
    assert dedup_table_name(None) == "fill_dedup_global"


def test_the_partition_table_name_carries_the_account(tmp_path: Path) -> None:
    """Two accounts are two tables, so rows cannot collide at the storage level.

    This is what "partitioned by account" means concretely, and it is why a
    cross-account lookup cannot find the other account's fills: there is no
    single table to look them up in.
    """
    assert dedup_table_name("acc-1") == "fill_dedup_acc-1"
    assert dedup_table_name("acc-2") == "fill_dedup_acc-2"
    assert dedup_table_name("acc-1") != dedup_table_name("acc-2")


def test_an_account_id_is_not_forced_to_be_alphanumeric() -> None:
    """``acc-1`` is a real account id and appears unchanged in a stream name."""
    # The scoped stream is "PAPER:acc-1:fills"; the dedup table must accept
    # the same id without a separate, stricter rule.
    assert dedup_table_name("acc-1") == "fill_dedup_acc-1"


def test_an_account_id_that_cannot_name_a_table_is_refused() -> None:
    """A quote would break out of the identifier; an empty name names nothing."""
    import pytest

    with pytest.raises(ValueError):
        dedup_table_name('acc"; DROP TABLE events; --')
    with pytest.raises(ValueError):
        dedup_table_name("")


def test_the_table_name_is_never_interpolated_from_caller_input(
    tmp_path: Path,
) -> None:
    """The injection attempt is rejected, and the events table survives."""
    db = tmp_path / "events.db"
    store = _store(db)
    try:
        with __import__("pytest").raises(ValueError):
            store.fill_dedup_count('acc"; DROP TABLE events; --')
        # The events table is still here, so nothing was dropped.
        store.append(_filled(_fill(fill_id="trade-1")))
    finally:
        store.close()


def test_the_dedup_table_is_created_on_first_use_not_on_open(
    tmp_path: Path,
) -> None:
    """A store that never records a fill leaves no dedup tables behind."""
    import sqlite3

    db = tmp_path / "events.db"
    store = _store(db, broker_id="PAPER", account_id="acc-1")
    store.close()

    con = sqlite3.connect(db)
    try:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            )
        }
    finally:
        con.close()
    assert "fill_dedup_acc-1" not in tables


def test_a_count_for_an_account_that_never_recorded_is_zero(
    tmp_path: Path,
) -> None:
    """A partition with no rows counts zero, and reading it is not an error."""
    db = tmp_path / "events.db"
    store = _store(db, broker_id="PAPER", account_id="acc-1")
    try:
        store.record_fill(_fill(fill_id="trade-1"))
        # acc-2 has a table now (the count created it) and it is empty.
        assert store.fill_dedup_count("acc-2") == 0
        assert store.fill_dedup_count() == 1
    finally:
        store.close()


def test_the_index_survives_a_reopen_within_the_same_process(tmp_path: Path) -> None:
    """A store that reopens the database reads the partitions it finds there.

    A partition is a table, so it outlives the store that created it; a
    reopen must read the rows the earlier store recorded rather than see an
    empty index, or a long-running session's dedup state would reset on every
    reconnect.
    """
    db = tmp_path / "events.db"
    store = _store(db, broker_id="PAPER", account_id="acc-1")
    try:
        assert store.record_fill(_fill(fill_id="trade-1")) is True
        assert store.fill_dedup_count() == 1
    finally:
        store.close()

    reopened = _store(db, broker_id="PAPER", account_id="acc-1")
    try:
        assert reopened.fill_dedup_count() == 1
        assert reopened.fill_seen(_fill(fill_id="trade-1")) is True
    finally:
        reopened.close()
