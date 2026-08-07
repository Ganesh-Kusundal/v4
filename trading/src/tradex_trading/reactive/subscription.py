"""DisposableSubscription bridge and SubscriptionManager.

Bridges RxPY Disposable with the trading session lifecycle for clean teardown.
"""

from __future__ import annotations

from rx.disposable import CompositeDisposable, Disposable


class DisposableSubscription:
    """Bridge between RxPY Disposable and the trading session lifecycle.

    Tracks subscriptions for clean teardown.  Supports context-manager usage::

        with DisposableSubscription() as sub:
            sub.add(disposable)
        # all subscriptions disposed here
    """

    def __init__(self) -> None:
        self._disposables: CompositeDisposable = CompositeDisposable()

    def add(self, disposable: Disposable) -> None:
        """Track a subscription for later cleanup."""
        self._disposables.add(disposable)

    def dispose(self) -> None:
        """Dispose all tracked subscriptions."""
        self._disposables.dispose()

    @property
    def is_disposed(self) -> bool:
        """Return True if all subscriptions have been disposed."""
        return self._disposables.is_disposed

    # Backward-compat alias (camelCase from spec)
    @property
    def isDisposed(self) -> bool:  # noqa: N802
        return self.is_disposed

    # Context-manager support
    def __enter__(self) -> DisposableSubscription:
        return self

    def __exit__(self, *args: object) -> None:
        self.dispose()


class SubscriptionManager:
    """Manages named subscription groups for selective teardown."""

    def __init__(self) -> None:
        self._groups: dict[str, DisposableSubscription] = {}

    def group(self, name: str) -> DisposableSubscription:
        """Get or create a named subscription group."""
        if name not in self._groups:
            self._groups[name] = DisposableSubscription()
        return self._groups[name]

    def dispose_group(self, name: str) -> None:
        """Dispose a specific subscription group."""
        group = self._groups.pop(name, None)
        if group is not None:
            group.dispose()

    def dispose_all(self) -> None:
        """Dispose all subscription groups."""
        for group in self._groups.values():
            group.dispose()
        self._groups.clear()
