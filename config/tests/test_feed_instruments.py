"""The operator names the tape: ``$TRADEX_FEED_INSTRUMENTS`` is a first class setting.

Lane c1 wired the cold start into ``boot()`` and proved it, then reported the
half it could not close: ``MarketFeed.start()`` requires a non-empty instrument
set and **the repository carried no watchlist** — no config field, no env path,
nothing on a strategy, artifact, scanner or CLI. A live boot therefore named no
tape, so the feed was never started and every order stayed refused with
``feed_not_ready``.

The decision is that an operator names the tape, and the platform refuses to
guess which instruments to trade. That makes the configuration surface the
deliverable, and these tests pin its contract:

* **empty is legal and is the default** — paper and backtest need no tape, so
  every construction path with the variable unset hands back an empty tape;
* **the form is ``EXCHANGE:SYMBOL``** and the exchange is not optional. A bare
  symbol names two different securities (``RELIANCE`` is an NSE cash equity and
  a BSE one, and this repository has already been bitten by exactly that
  collision), so a bare symbol is a configuration error, not a default to NSE;
* **a malformed value is a loud configuration error, never a silently empty
  tape** — the failure mode that would leave a live session refusing every
  order for a reason nobody was told;
* **duplicates and stray whitespace are normalised**, order preserved, so the
  same tape is proven once;
* **resolution is per call**, the same convention as ``default_runtime_dir_str``:
  a module constant would freeze the first value seen and go stale for anything
  that sets the variable later;
* **an explicit empty tape is still expressible**, so a caller that knows better
  than the environment can say "no tape" and get the refusal rather than a
  surprise.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from tradex_config.env import from_env
from tradex_config.schema import AppConfig, parse_feed_instruments
from tradex_domain import Equity

_VAR = "TRADEX_FEED_INSTRUMENTS"


def _ids(instruments: Iterable[object]) -> list[str]:
    return [str(i.instrument_id) for i in instruments]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 1. Unset is empty, everywhere a config can be read
# ---------------------------------------------------------------------------


def test_unset_is_an_empty_tape_on_every_read_path(monkeypatch) -> None:
    """No variable, no tape — and paper is the mode that must not notice.

    ``AppConfig()`` and ``from_env()`` are the two shapes a caller actually
    uses, and they must agree: a default that only the loader honours would
    leave every hand-built config trading a tape the operator never named.
    """
    monkeypatch.delenv(_VAR, raising=False)

    assert AppConfig().feed_instruments == ()
    assert from_env().feed_instruments == ()
    assert AppConfig.from_dict({}).feed_instruments == ()


def test_unset_keeps_the_paper_default_intact(monkeypatch) -> None:
    """The control that matters most: paper with no variable is unchanged."""
    monkeypatch.delenv(_VAR, raising=False)
    monkeypatch.setenv("TRADEX_MODE", "paper")

    cfg = from_env()

    assert cfg.mode == "paper"
    assert cfg.broker_id.value == "PAPER"
    assert cfg.feed_instruments == ()


# ---------------------------------------------------------------------------
# 2. The variable names the tape
# ---------------------------------------------------------------------------


def test_a_comma_separated_list_becomes_instruments_in_order(monkeypatch) -> None:
    monkeypatch.setenv(_VAR, "NSE:RELIANCE,NSE:TCS")

    assert _ids(from_env().feed_instruments) == ["NSE:RELIANCE", "NSE:TCS"]
    assert _ids(AppConfig().feed_instruments) == ["NSE:RELIANCE", "NSE:TCS"]


def test_whitespace_duplicates_and_empty_slots_are_normalised(monkeypatch) -> None:
    """``"  nse:reliance , NSE:RELIANCE , , NSE:TCS,"`` is two instruments.

    Empty slots are dropped rather than becoming an instrument with no symbol —
    a trailing comma is a typo, not a request to trade nothing. Lower case and
    stray spaces are the operator's, and the domain already owns symbol
    normalisation, so this delegates rather than re-implementing it.
    """
    monkeypatch.setenv(_VAR, "  nse:reliance , NSE:RELIANCE ,, NSE:TCS,")

    assert _ids(from_env().feed_instruments) == ["NSE:RELIANCE", "NSE:TCS"]


def test_resolution_is_per_call_not_frozen_at_import(monkeypatch) -> None:
    """A late ``setenv`` is observed, the ``runtime_dir`` convention.

    A module constant would capture the first value the process ever saw and go
    stale for anything that sets the variable afterwards, which is how the two
    runtime roots forked.
    """
    monkeypatch.delenv(_VAR, raising=False)
    assert AppConfig().feed_instruments == ()

    monkeypatch.setenv(_VAR, "NSE:TCS")
    assert _ids(AppConfig().feed_instruments) == ["NSE:TCS"]
    assert _ids(from_env().feed_instruments) == ["NSE:TCS"]


def test_an_explicit_empty_tape_overrides_the_environment(monkeypatch) -> None:
    """A caller may still say "no tape" and get the refusal, not a surprise."""
    monkeypatch.setenv(_VAR, "NSE:TCS")

    assert AppConfig(feed_instruments=()).feed_instruments == ()


# ---------------------------------------------------------------------------
# 3. A malformed value is an error, not a silently empty tape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "offending"),
    [
        ("RELIANCE", "RELIANCE"),  # bare symbol: the exchange is not optional
        ("NSE:", "NSE:"),  # no symbol
        ("NSE::RELIANCE", "NSE::RELIANCE"),  # empty middle
        ("XYZ:TCS", "XYZ:TCS"),  # no such exchange
        ("NSE:RELIANCE:20260101:FUT", "NSE:RELIANCE:20260101:FUT"),  # not a cash tape
    ],
)
def test_a_malformed_entry_is_a_configuration_error(
    monkeypatch, value: str, offending: str
) -> None:
    """The error must name the variable and the entry, or it teaches nothing.

    The alternative — dropping the unparseable entry and starting with whatever
    survived — is the dangerous one: the session would come up trading a tape
    the operator did not write, with the typo discarded in silence.
    """
    monkeypatch.setenv(_VAR, f"NSE:TCS,{offending}")

    with pytest.raises(ValueError) as excinfo:
        from_env()

    message = str(excinfo.value)
    assert _VAR in message
    assert offending in message


def test_a_whole_value_of_separators_is_empty_not_an_error(monkeypatch) -> None:
    """``","`` named nothing, which is legal and means nothing to stream."""
    monkeypatch.setenv(_VAR, " , , ")

    assert from_env().feed_instruments == ()


# ---------------------------------------------------------------------------
# 4. The text form is the same on the dict path
# ---------------------------------------------------------------------------


def test_from_dict_takes_the_same_text_form_and_de_duplicates() -> None:
    """JSON carries strings, so ``from_dict`` speaks the language of the env var."""
    cfg = AppConfig.from_dict({"feed_instruments": ["NSE:TCS", " nse:tcs ", "NSE:RELIANCE"]})

    assert _ids(cfg.feed_instruments) == ["NSE:TCS", "NSE:RELIANCE"]


def test_from_dict_rejects_a_malformed_entry() -> None:
    with pytest.raises(ValueError, match=_VAR):
        AppConfig.from_dict({"feed_instruments": ["NSE:TCS", "TCS"]})


def test_from_dict_still_rejects_unknown_sections() -> None:
    """Adding a key must not have opened the strictness up."""
    with pytest.raises(ValueError, match="unknown config sections"):
        AppConfig.from_dict({"feed_instruments_typo": []})


# ---------------------------------------------------------------------------
# 5. The constructor holds the invariant whatever the source
# ---------------------------------------------------------------------------


def test_the_constructor_de_duplicates_instrument_objects() -> None:
    one, two = Equity.of("NSE", "RELIANCE"), Equity.of("NSE", "TCS")

    cfg = AppConfig(feed_instruments=[one, one, two])

    assert cfg.feed_instruments == (one, two), "a tape is proven once per instrument"


def test_the_constructor_rejects_text_and_junk_with_the_variable_named() -> None:
    """Instruments go in as objects; text goes through the parser or the env var.

    Accepting a string here would give the field two input forms, and iterating
    the characters of ``"NSE:TCS"`` would be the failure mode nobody expects.
    """
    with pytest.raises(ValueError, match=_VAR):
        AppConfig(feed_instruments=["NSE:TCS"])  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=_VAR):
        AppConfig(feed_instruments="NSE:TCS")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="NSE:TCS"):
        AppConfig(feed_instruments="NSE:TCS")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=_VAR):
        AppConfig(feed_instruments=[Equity])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# 6. The parser itself, as a first class part of the surface
# ---------------------------------------------------------------------------


def test_the_parser_accepts_either_text_form() -> None:
    assert _ids(parse_feed_instruments("NSE:TCS")) == ["NSE:TCS"]
    assert _ids(parse_feed_instruments(["NSE:TCS", "NSE:TCS"])) == ["NSE:TCS"]
    assert parse_feed_instruments("") == ()


# ---------------------------------------------------------------------------
# 7. The unattended operator path: a line in .env.local
# ---------------------------------------------------------------------------


def test_the_setting_survives_the_env_file_loader(monkeypatch, tmp_path) -> None:
    """``load_env_file`` is how the live scripts read ``.env.local``.

    ``quick_start_live.py`` calls it before booting, so a tape named there has
    to arrive as one comma separated value — a value the generic ``KEY=VALUE``
    loader must not mangle, quote or split.
    """
    from tradex_config.env import load_env_file

    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "# the tape this account trades\n"
        "TRADEX_FEED_INSTRUMENTS = NSE:RELIANCE, NSE:TCS\n"
    )
    monkeypatch.delenv(_VAR, raising=False)

    assert load_env_file(env_file) == (_VAR,)

    assert _ids(from_env().feed_instruments) == ["NSE:RELIANCE", "NSE:TCS"]
    assert _ids(AppConfig().feed_instruments) == ["NSE:RELIANCE", "NSE:TCS"]
