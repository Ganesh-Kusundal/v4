from tradex_persistence import OrderStore, SQLiteEventStore


def test_import_package() -> None:
    assert SQLiteEventStore is not None
    assert OrderStore is not None
