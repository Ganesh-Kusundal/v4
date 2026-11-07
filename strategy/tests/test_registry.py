"""Tests for strategy registry."""

from tradex_strategy.base import BaseStrategy
from tradex_strategy.registry import register, get, create, list_strategies, clear


def setup_function():
    clear()


def test_register_and_get():
    @register("test_strat")
    class MyStrat(BaseStrategy):
        def on_bar(self, bar, ctx):
            return []

    assert get("test_strat") is MyStrat


def test_create_instance():
    @register("test_strat2")
    class MyStrat2(BaseStrategy):
        def on_bar(self, bar, ctx):
            return []

    inst = create("test_strat2", params={"x": 5})
    assert inst.params["x"] == 5
    assert inst.name == "MyStrat2"


def test_list_strategies():
    @register("a")
    class A(BaseStrategy):
        def on_bar(self, bar, ctx):
            return []

    @register("b")
    class B(BaseStrategy):
        def on_bar(self, bar, ctx):
            return []

    assert list_strategies() == ["a", "b"]


def test_unknown_raises():
    import pytest
    with pytest.raises(KeyError):
        create("nonexistent")


def test_clear():
    @register("x")
    class X(BaseStrategy):
        def on_bar(self, bar, ctx):
            return []

    clear()
    assert list_strategies() == []
