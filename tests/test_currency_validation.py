"""Recognized currencies only, at every input door (ADR-0009).

One invariant: no currency code crosses apply_intent unvalidated. Unknown codes
reject loudly and are never re-interpreted; stored data is never re-policed.
"""

from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Expense, Ledger
from tests.factories import bot_added_update, message_update, user

ALICE = user(1001, "Alice", "alice")
BOB = user(1002, "Bob", "bob")

REFUSAL = "isn't a currency I know"


async def setup_group(deps, chat_id: int = -42) -> None:
    await dispatch(bot_added_update(chat_id=chat_id, by=ALICE), deps)
    await dispatch(message_update(update_id=90, chat_id=chat_id, text="/homecurrency EUR"), deps)
    await dispatch(message_update(update_id=91, chat_id=chat_id, text="hi", from_user=BOB), deps)


async def reply_to(deps, text: str, update_id: int = 200) -> str:
    outbound = await dispatch(message_update(update_id=update_id, chat_id=-42, text=text), deps)
    [reply] = [a for a in outbound if a.kind == "send_message"]
    return reply.text


async def test_homecurrency_rejects_an_unrecognized_code(deps):
    await dispatch(bot_added_update(chat_id=-42, by=ALICE), deps)

    text = await reply_to(deps, "/homecurrency FUN")

    assert REFUSAL in text
    # the flip rolled back: a later /equal still has no currency to resolve
    text = await reply_to(deps, "/equal 30 dinner @alice", update_id=201)
    assert "Set a currency first" in text


async def test_currency_rejects_an_unrecognized_code(deps):
    await setup_group(deps)

    text = await reply_to(deps, "/currency FUN")

    assert REFUSAL in text
    async with deps.session_factory() as session:
        [ledger] = (await session.execute(select(Ledger))).scalars()
    assert ledger.logging_currency is None


async def test_newledger_rejects_an_unrecognized_trailing_code_and_creates_nothing(deps):
    """ADR-0009: reject loudly — the token is never folded back into the name."""
    await setup_group(deps)

    text = await reply_to(deps, "/newledger Tokyo JPZ")

    assert REFUSAL in text and "JPZ" in text
    async with deps.session_factory() as session:
        ledgers = list((await session.execute(select(Ledger))).scalars())
    assert [ledger.name for ledger in ledgers] == ["Japan Trip"]  # no Tokyo, no "Tokyo JPZ"


async def test_a_lone_iso_looking_token_still_names_a_ledger(deps):
    """'/newledger USD' names a ledger USD and sets nothing (ADR-0009 keeps this)."""
    await setup_group(deps)

    text = await reply_to(deps, "/newledger USD")

    assert "USD is now the active ledger" in text
    async with deps.session_factory() as session:
        usd = (await session.execute(select(Ledger).where(Ledger.name == "USD"))).scalar_one()
    assert usd.logging_currency is None


async def test_expense_override_rejects_an_unrecognized_code(deps):
    """'/equal 30 EUE dinner' corrects the typo instead of minting an EUE bucket."""
    await setup_group(deps)

    text = await reply_to(deps, "/equal 30 EUE dinner @alice @bob")

    assert REFUSAL in text and "EUE" in text
    async with deps.session_factory() as session:
        assert list((await session.execute(select(Expense))).scalars()) == []


async def test_every_expense_split_command_validates_the_override(deps):
    await setup_group(deps)

    for text in (
        "/exact 30 EUE dinner @alice=10 @bob=20",
        "/shares 30 EUE dinner @alice @bob",
        "/percent 30 EUE dinner @alice=50 @bob=50",
    ):
        assert REFUSAL in await reply_to(deps, text), text


async def test_stored_codes_are_never_re_policed(deps):
    """Inputs only (ADR-0009): a retired code in history keeps rendering and balancing."""
    await setup_group(deps)
    await dispatch(
        message_update(update_id=95, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )
    async with deps.session_factory() as session, session.begin():
        # history, not a keystroke: as an import (slice 17) or a pre-ADR-0009 row
        # would leave it — HRK retired when Croatia adopted the euro
        [expense] = (await session.execute(select(Expense))).scalars().all()
        expense.currency = "HRK"

    text = await reply_to(deps, "/balance")

    assert "HRK" in text  # replayed and rendered, not rejected
