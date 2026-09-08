def test_historical_sync_benchmark_uses_current_orchestrator():
    import benchmarks.bench_historical_sync as benchmark

    assert benchmark.SyncOrchestrator is not None
