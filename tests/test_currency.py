import pytest

from expensir.domain.currency import CannotResolveCurrency, resolve_currency


def test_explicit_override_wins():
    assert resolve_currency("SGD", "JPY", "USD") == "SGD"


def test_ledger_logging_currency_beats_group_home():
    assert resolve_currency(None, "JPY", "USD") == "JPY"


def test_home_currency_is_the_fallback():
    assert resolve_currency(None, None, "USD") == "USD"


def test_nothing_resolvable_raises_with_guidance():
    with pytest.raises(CannotResolveCurrency, match="/currency|/homecurrency"):
        resolve_currency(None, None, None)


def test_codes_are_normalized_upper():
    assert resolve_currency("sgd", None, None) == "SGD"
