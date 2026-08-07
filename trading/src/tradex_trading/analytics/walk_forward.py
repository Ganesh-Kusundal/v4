"""Walk-forward window splitting."""

from __future__ import annotations


def split_windows(
    n: int,
    train: int,
    test: int,
    step: int | None = None,
) -> list[tuple[int, int, int, int]]:
    """Yield (train_start, train_end, test_start, test_end) half-open indices.

    Default step equals the test size: non-overlapping test windows with a
    rolling train window.
    """
    if train <= 0 or test <= 0 or n < train + test:
        return []
    stride = step if step is not None else test
    out: list[tuple[int, int, int, int]] = []
    start = 0
    while start + train + test <= n:
        train_end = start + train
        out.append((start, train_end, train_end, train_end + test))
        start += stride
    return out


__all__ = ["split_windows"]
