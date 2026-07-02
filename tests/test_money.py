import pytest

from expensir.domain.money import fmt, to_minor


def test_parses_two_decimal_currencies_to_cents():
    assert to_minor("60", "USD") == (6000, False)
    assert to_minor("60.50", "USD") == (6050, False)


def test_parses_zero_decimal_currencies_to_whole_units():
    assert to_minor("6000", "JPY") == (6000, False)


def test_parses_three_decimal_currencies_to_mils():
    assert to_minor("6.000", "BHD") == (6000, False)
    assert to_minor("6.5", "KWD") == (6500, False)


def test_unknown_currency_defaults_to_two_minor_digits():
    assert to_minor("12.34", "XXX") == (1234, False)


def test_over_precise_input_rounds_half_up_and_flags_it():
    assert to_minor("6000.50", "JPY") == (6001, True)
    assert to_minor("6000.49", "JPY") == (6000, True)
    assert to_minor("10.005", "USD") == (1001, True)


def test_formats_with_the_currency_minor_digits():
    assert fmt(6050, "USD") == "USD 60.50"
    assert fmt(6001, "JPY") == "JPY 6001"
    assert fmt(6500, "KWD") == "KWD 6.500"
    assert fmt(-3000, "USD") == "USD -30.00"


def test_parse_format_round_trip():
    minor, _ = to_minor("1234.56", "USD")
    assert fmt(minor, "USD") == "USD 1234.56"


def test_rejects_garbage_and_non_positive_amounts():
    for bad in ("abc", "", "12.3.4", "-5", "0"):
        with pytest.raises(ValueError):
            to_minor(bad, "USD")


def test_rejects_amounts_too_large_to_store():
    # anything past the cap would overflow the DB integer at flush time and
    # crash the update pipeline instead of replying (slice-2 review finding)
    with pytest.raises(ValueError, match="too large"):
        to_minor("100000000000000000", "USD")
    # the cap itself still parses
    assert to_minor("10000000000000", "USD") == (10**15, False)


def test_rejects_amounts_that_round_to_zero_minor_units():
    # 0.004 USD is positive but smaller than the smallest cent — allocating it
    # would fail with an internal error; reject with the smallest-unit message
    with pytest.raises(ValueError, match="smallest"):
        to_minor("0.004", "USD")
    with pytest.raises(ValueError, match="smallest"):
        to_minor("0.4", "JPY")
