"""The in-process and durable fill fingerprints must be one rule, not two."""

from tradex_execution.idempotency import fingerprints_agree


def test_lru_and_store_fingerprints_agree() -> None:
    assert fingerprints_agree(), (
        "FillDedup.fingerprint and sqlite_event_store.fill_fingerprint have "
        "drifted: the same fill would read as fresh in the LRU and duplicate "
        "in the store (or the reverse), so dedup depends on whether a store "
        "happens to be configured."
    )
