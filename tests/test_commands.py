import pytest

from expensir.intents.commands import ParsedExpense, parse_equal, parse_homecurrency


def test_parse_equal_with_named_participants():
    parsed = parse_equal("/equal 60 dinner @alice @bob")
    assert parsed == ParsedExpense(
        amount="60",
        currency=None,
        description="dinner",
        participant_refs=["@alice", "@bob"],
        split_type="equal",
    )


def test_parse_equal_with_iso_override_after_amount():
    parsed = parse_equal("/equal 30 SGD train tickets @alice")
    assert parsed.currency == "SGD"
    assert parsed.amount == "30"
    assert parsed.description == "train tickets"


def test_lowercase_three_letter_words_are_description_not_iso():
    parsed = parse_equal("/equal 30 fun stuff @alice")
    assert parsed.currency is None
    assert parsed.description == "fun stuff"


def test_parse_equal_with_no_participants_means_everyone():
    parsed = parse_equal("/equal 12.50 coffee")
    assert parsed.participant_refs == []
    assert parsed.amount == "12.50"


def test_parse_equal_mentions_may_be_interleaved():
    parsed = parse_equal("/equal 60 @alice dinner @bob downtown")
    assert parsed.participant_refs == ["@alice", "@bob"]
    assert parsed.description == "dinner downtown"


def test_parse_equal_rejects_missing_amount_or_description():
    with pytest.raises(ValueError, match="[Uu]sage"):
        parse_equal("/equal")
    with pytest.raises(ValueError, match="[Uu]sage"):
        parse_equal("/equal 60 @alice")
    with pytest.raises(ValueError, match="[Uu]sage"):
        parse_equal("/equal dinner @alice")


def test_parse_homecurrency():
    assert parse_homecurrency("/homecurrency SGD") == "SGD"
    assert parse_homecurrency("/homecurrency usd") == "USD"
    with pytest.raises(ValueError, match="[Uu]sage"):
        parse_homecurrency("/homecurrency")
    with pytest.raises(ValueError, match="[Uu]sage"):
        parse_homecurrency("/homecurrency dollars")
