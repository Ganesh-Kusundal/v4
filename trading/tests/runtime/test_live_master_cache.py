"""Live instrument-master cache wiring.

``build_*_from_env`` must attach a durable ``MasterFileCache`` to the broker's
master loader for real (non-injected) connects so the 100k+-row instrument
master is downloaded once per TTL instead of on every connect. Injected
fetches (tests) stay cache-free unless a directory is given explicitly.
"""

from __future__ import annotations

from pathlib import Path

from tradex_trading.runtime.live import _master_cache, build_dhan_from_env, build_upstox_from_env


def _injected_fetch(*_args: object, **_kwargs: object) -> tuple[int, object]:
    return 200, {}


class TestMasterCacheResolution:
    """_master_cache picks the cache root by fetch mode."""

    def test_real_fetch_defaults_to_runtime_dir(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("TRADEX_RUNTIME_DIR", str(tmp_path))
        cache = _master_cache("dhan", ".json", fetch=None, master_cache_dir=None)
        assert cache is not None
        path = cache.cache_path()
        assert path.parent == tmp_path
        assert path.name.startswith("dhan-instruments-")
        assert path.name.endswith(".json")

    def test_injected_fetch_stays_cache_free(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("TRADEX_RUNTIME_DIR", str(tmp_path))
        cache = _master_cache("dhan", ".json", fetch=_injected_fetch, master_cache_dir=None)
        assert cache is None

    def test_explicit_dir_wins_for_injected_fetch(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        cache = _master_cache(
            "upstox", ".json", fetch=_injected_fetch, master_cache_dir=cache_dir
        )
        assert cache is not None
        assert cache.cache_path().parent == cache_dir


class TestLiveBuildersAttachCache:
    """build_*_from_env wire a durable cache for real connects only."""

    def test_dhan_real_connect_attaches_runtime_cache(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("TRADEX_RUNTIME_DIR", str(tmp_path))
        monkeypatch.setenv("DHAN_CLIENT_ID", "test-client")
        monkeypatch.setenv("DHAN_ACCESS_TOKEN", "test-token")
        broker = build_dhan_from_env(load_instruments=True)
        assert broker.master_loader is not None
        cache = broker.master_loader._cache  # type: ignore[attr-defined]
        assert cache is not None
        assert cache.cache_path().parent == tmp_path

    def test_dhan_injected_fetch_builds_cache_free(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("TRADEX_RUNTIME_DIR", str(tmp_path))
        monkeypatch.setenv("DHAN_CLIENT_ID", "test-client")
        monkeypatch.setenv("DHAN_ACCESS_TOKEN", "test-token")
        broker = build_dhan_from_env(fetch=_injected_fetch, load_instruments=True)
        assert broker.master_loader is not None
        assert broker.master_loader._cache is None  # type: ignore[attr-defined]

    def test_upstox_real_connect_attaches_runtime_cache(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("TRADEX_RUNTIME_DIR", str(tmp_path))
        monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "test-token")
        broker = build_upstox_from_env(load_instruments=True)
        assert broker.master_loader is not None
        cache = broker.master_loader._cache  # type: ignore[attr-defined]
        assert cache is not None
        assert cache.cache_path().parent == tmp_path


class _CountingFetch:
    """Injected fetch that counts downloads and serves a synthetic Dhan master."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls = 0

    def __call__(self, *_args: object, **_kwargs: object) -> tuple[int, object]:
        self.calls += 1
        return 200, self.body


def _big_dhan_master_csv() -> bytes:
    """Synthetic Dhan master above the 10k-row strict floor."""
    header = (
        "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_TRADING_SYMBOL,"
        "SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME\n"
    )
    rows = [
        f"NSE,E,SYM{i},{i},SYM{i}\n" for i in range(10_000)
    ]
    return (header + "".join(rows)).encode("utf-8")


class TestWarmCacheReuse:
    """A warm on-disk cache prevents re-downloading the master."""

    def test_dhan_second_load_serves_from_disk(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("DHAN_CLIENT_ID", "test-client")
        monkeypatch.setenv("DHAN_ACCESS_TOKEN", "test-token")
        counting = _CountingFetch(_big_dhan_master_csv())
        broker = build_dhan_from_env(
            fetch=counting, load_instruments=True, master_cache_dir=tmp_path
        )
        assert broker.master_loader is not None

        cache = broker.master_loader._cache  # type: ignore[attr-defined]
        first = broker.master_loader.load()
        second = broker.master_loader.load()

        assert counting.calls == 1  # warm cache: no second download
        assert len(first) == len(second) == 10_000
        assert cache is not None
        assert cache.cache_path().exists()
