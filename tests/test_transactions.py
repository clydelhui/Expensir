"""/transactions page 1 (#24, ADR-0012): the merged history read + shared primitives."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Action, Expense, Group, Ledger, Settlement
from expensir.db.models import User as DbUser
from expensir.domain.transactions import (
    SettlementRow,
    TransactionCursor,
    TransactionPage,
    decode_cursor,
    encode_cursor,
    list_transactions,
)
from expensir.format.keyboards import transactions_pager_keyboard
from tests.factories import bot_added_update, callback_update, message_update, user

ALICE = user(1001, "Alice", "alice")
BOB = user(1002, "Bob", "bob")


async def two_member_group(deps, chat_id: int = -42) -> None:
    await dispatch(bot_added_update(chat_id=chat_id, by=ALICE), deps)
    await dispatch(message_update(update_id=90, chat_id=chat_id, text="hi", from_user=BOB), deps)


async def say(deps, text: str, *, from_user=ALICE, update_id: int = 100, message_id: int = 100):
    return await dispatch(
        message_update(
            update_id=update_id,
            chat_id=-42,
            text=text,
            from_user=from_user,
            message_id=message_id,
        ),
        deps,
    )


async def test_transactions_renders_header_with_count_and_expenses_newest_first(deps):
    await two_member_group(deps)
    await say(deps, "/equal 60 EUR dinner", update_id=101, message_id=101)
    await say(deps, "/equal 30 EUR taxi", update_id=102, message_id=102)

    [reply] = await say(deps, "/transactions", update_id=103, message_id=103)

    header, *blocks = reply.text.split("\n\n")
    assert "Japan Trip" in header
    assert "2 transactions" in header
    assert "newest first" in header
    assert "taxi" in blocks[0] and "EUR 30.00" in blocks[0]
    assert "dinner" in blocks[1] and "EUR 60.00" in blocks[1]


async def test_settlements_merge_into_the_stream_ordered_by_created_at_desc(deps):
    await two_member_group(deps)
    await say(deps, "/equal 60 EUR dinner", update_id=101, message_id=101)
    await say(deps, "/settle @bob 20 EUR", update_id=102, message_id=102)
    await say(deps, "/equal 30 EUR taxi", update_id=103, message_id=103)

    [reply] = await say(deps, "/transactions", update_id=104, message_id=104)

    header, *blocks = reply.text.split("\n\n")
    assert "3 transactions" in header
    assert "taxi" in blocks[0]
    assert "Alice paid Bob" in blocks[1] and "EUR 20.00" in blocks[1]
    assert "dinner" in blocks[2]


async def test_occurred_on_is_displayed_but_never_reorders_and_edits_are_marked(deps):
    await two_member_group(deps)
    await dispatch(
        message_update(update_id=95, chat_id=-42, text="/homecurrency USD", from_user=ALICE), deps
    )
    await say(deps, "/equal 60 EUR dinner", update_id=101, message_id=101)
    await say(deps, "/equal 30 EUR taxi", update_id=102, message_id=102)
    # back-date dinner to long before taxi: display changes, order must not (§7.2)
    await say(deps, "/edit #1 2026-01-15", update_id=103, message_id=103)

    [reply] = await say(deps, "/transactions", update_id=104, message_id=104)

    header, taxi, dinner = reply.text.split("\n\n")
    assert "dinner" in dinner  # still below taxi: created_at DESC, not occurred_on
    first_line, second_line = dinner.splitlines()
    assert first_line == "#1 dinner — EUR 60.00"
    assert second_line.startswith("2026-01-15")
    assert "✏️ edited" in second_line
    assert "paid by Alice, split 2 ways" in second_line
    assert "✏️" not in taxi  # only the edited row is marked
    assert "≈" not in reply.text  # native amounts only, no home-currency equivalents


async def test_soft_deleted_transactions_are_excluded_from_rows_and_count(deps):
    await two_member_group(deps)
    await say(deps, "/equal 60 EUR dinner", update_id=101, message_id=101)
    await say(deps, "/equal 30 EUR taxi", update_id=102, message_id=102)
    await say(deps, "/delete #1", update_id=103, message_id=103)

    [reply] = await say(deps, "/transactions", update_id=104, message_id=104)

    header, *blocks = reply.text.split("\n\n")
    assert "1 transaction," in header  # count skips the deleted row (and is singular)
    assert len(blocks) == 1
    assert "dinner" not in reply.text


async def test_empty_ledger_gets_a_friendly_nudge_with_no_keyboard(deps):
    await two_member_group(deps)

    [reply] = await say(deps, "/transactions", update_id=101, message_id=101)

    assert "No transactions yet" in reply.text
    assert "Japan Trip" in reply.text
    assert reply.reply_markup is None


async def test_transactions_is_a_read_writing_no_new_action_and_carrying_no_undo(deps):
    await two_member_group(deps)
    await say(deps, "/equal 60 EUR dinner", update_id=101, message_id=101)
    async with deps.session_factory() as session:
        before = (await session.execute(select(Action.id))).scalars().all()

    [reply] = await say(deps, "/transactions", update_id=102, message_id=102)

    assert reply.reply_markup is None  # ≤10 transactions: no pager, and never an Undo
    async with deps.session_factory() as session:
        after = (await session.execute(select(Action.id))).scalars().all()
    assert after == before  # a read appends no action row


async def test_transactions_with_arguments_is_rejected_with_usage(deps):
    await two_member_group(deps)

    [reply] = await say(deps, "/transactions all", update_id=101, message_id=101)

    assert "Usage" in reply.text
    assert "newest first" in reply.text  # the usage line explains what it does


async def test_more_than_ten_transactions_render_an_older_pager(deps):
    reply, _ = await eleven_expense_listing(deps)

    header, *blocks = reply.text.split("\n\n")
    assert "11 transactions" in header
    assert len(blocks) == 10  # page size 10: the oldest row waits behind ▶
    assert "item0" not in reply.text
    assert reply.reply_markup is not None
    [[older]] = reply.reply_markup["inline_keyboard"]  # page 1: no ◀, only ▶
    assert "Older" in older["text"]
    # the versioned cursor grammar (ADR-0012): v1:tx:<ledger>:<n|p>:<epoch_us>:<kind>:<row_id>
    version, noun, _ledger, verb, epoch_us, kind, row_id = older["callback_data"].split(":")
    assert (version, noun) == ("v1", "tx")
    assert verb == "n" and kind == "expense" and row_id == "2"  # anchored on the edge row
    assert epoch_us.isdigit()
    assert len(older["callback_data"].encode()) <= 64


def pager_buttons(markup) -> dict[str, str]:
    """The pager row keyed by direction: {'newer': data, 'older': data}."""
    assert markup is not None
    [row] = markup["inline_keyboard"]
    keys = {"◀ Newer": "newer", "▶ Older": "older"}
    return {keys[b["text"]]: b["callback_data"] for b in row}


async def eleven_expense_listing(deps):
    """A group with 11 expenses and its /transactions listing: page 1 + its ▶ data."""
    await two_member_group(deps)
    for n in range(11):
        await say(deps, f"/equal 10 EUR item{n}", update_id=101 + n, message_id=101 + n)
    [reply] = await say(deps, "/transactions", update_id=200, message_id=200)
    return reply, pager_buttons(reply.reply_markup)["older"]


async def tap(deps, data: str, *, update_id: int, message_id: int = 555):
    return await dispatch(
        callback_update(update_id=update_id, chat_id=-42, data=data, message_id=message_id), deps
    )


async def test_older_tap_edits_the_listing_message_in_place_with_the_next_page(deps):
    _, older = await eleven_expense_listing(deps)

    ack, edit = await tap(deps, older, update_id=201)

    assert type(ack).__name__ == "AnswerCallbackQuery"
    assert type(edit).__name__ == "EditMessage"
    assert (edit.chat_id, edit.message_id) == (-42, 555)  # the tapped message, in place
    header, *blocks = edit.text.split("\n\n")
    assert "11 transactions" in header  # header rides along with every page
    assert len(blocks) == 1 and "item0" in blocks[0]  # the one row behind page 1
    assert pager_buttons(edit.reply_markup).keys() == {"newer"}  # last page: ▶ trimmed


async def test_older_then_newer_round_trips_without_repeats_or_skips_despite_inserts(deps):
    _, older = await eleven_expense_listing(deps)

    _, page2 = await tap(deps, older, update_id=201)
    assert "item0" in page2.text
    # a new transaction lands while page 2 is on screen: it sits ABOVE the ◀
    # anchor, so paging back must neither repeat item0 nor skip item10..item1
    await say(deps, "/equal 10 EUR item11", update_id=202, message_id=202)

    _, back = await tap(deps, pager_buttons(page2.reply_markup)["newer"], update_id=203)

    header, *blocks = back.text.split("\n\n")
    assert "12 transactions" in header  # the header count refreshes with the page
    assert [b.split()[0] for b in blocks] == [f"#{n}" for n in range(11, 1, -1)]  # item10..item1
    buttons = pager_buttons(back.reply_markup)
    assert buttons.keys() == {"newer", "older"}  # item11 waits above, item0 below

    _, top = await tap(deps, buttons["newer"], update_id=204)
    header, *blocks = top.text.split("\n\n")
    assert len(blocks) == 1 and "item11" in blocks[0]
    assert pager_buttons(top.reply_markup).keys() == {"older"}  # first page: ◀ trimmed


async def test_pager_keeps_paging_the_rendered_ledger_across_a_concurrent_switch(deps):
    _, older = await eleven_expense_listing(deps)
    # the group moves on to a fresh ledger while the listing is on screen
    await say(deps, "/newledger Tokyo", update_id=201, message_id=201)
    await say(deps, "/equal 99 EUR sushi", update_id=202, message_id=202)

    _, edit = await tap(deps, older, update_id=203)

    # the ledger id pinned at render time wins: still Japan Trip's stream
    header, *blocks = edit.text.split("\n\n")
    assert "Japan Trip" in header and "11 transactions" in header
    assert "item0" in blocks[0]
    assert "sushi" not in edit.text


async def test_past_the_end_older_tap_offers_a_way_back_after_deletions(deps):
    _, older = await eleven_expense_listing(deps)
    # the only row behind ▶ vanishes while the button is on screen
    await say(deps, "/delete #1", update_id=201, message_id=201)

    _, edit = await tap(deps, older, update_id=202)

    assert "No older transactions" in edit.text
    assert pager_buttons(edit.reply_markup).keys() == {"newer"}  # ◀ still offered

    # the ◀ anchors on the tapped cursor: everything strictly newer than it,
    # with the anchor row itself (item1) still reachable behind ▶
    _, back = await tap(deps, pager_buttons(edit.reply_markup)["newer"], update_id=203)
    header, *blocks = back.text.split("\n\n")
    assert "10 transactions" in header  # the count refreshed past the deletion
    assert len(blocks) == 9 and "item10" in blocks[0] and "item2" in blocks[-1]
    assert pager_buttons(back.reply_markup).keys() == {"older"}


async def test_tap_on_a_fully_emptied_ledger_lands_on_the_empty_nudge(deps):
    await two_member_group(deps)
    await say(deps, "/equal 10 EUR item0", update_id=101, message_id=101)
    for n in range(1, 11):
        await say(deps, f"/equal 10 EUR item{n}", update_id=101 + n, message_id=101 + n)
    [reply] = await say(deps, "/transactions", update_id=200, message_id=200)
    older = pager_buttons(reply.reply_markup)["older"]
    for n in range(11):
        await say(deps, f"/delete #{n + 1}", update_id=300 + n, message_id=300 + n)

    _, edit = await tap(deps, older, update_id=400)

    assert "No transactions yet" in edit.text  # not a bare '0 transactions' dead end
    assert edit.reply_markup is None  # nothing on either side: no way-back button


async def test_tap_stranded_past_the_end_resets_to_the_first_page(deps):
    _, older = await eleven_expense_listing(deps)
    # every row except the ▶ anchor itself (#2/item1) vanishes: nothing is
    # strictly beyond the anchor on either side, yet the ledger still stands
    for update_id, n in enumerate([1, *range(3, 12)], start=300):
        await say(deps, f"/delete #{n}", update_id=update_id, message_id=update_id)

    _, edit = await tap(deps, older, update_id=400)

    header, *blocks = edit.text.split("\n\n")
    assert "1 transaction," in header  # not a dead-end page hiding the survivor
    assert len(blocks) == 1 and "item1" in blocks[0]
    assert edit.reply_markup is None  # one row, one page: nothing to page to


async def test_forged_cursor_with_an_overflowing_epoch_is_ignored_with_an_ack(deps):
    await two_member_group(deps)

    actions = await tap(deps, f"v1:tx:1:n:{10**30}:expense:1", update_id=201)

    assert [type(a).__name__ for a in actions] == ["AnswerCallbackQuery"]


async def test_tap_carrying_another_groups_ledger_answers_without_editing(deps):
    _, older = await eleven_expense_listing(deps)  # Japan Trip lives in chat -42
    await dispatch(bot_added_update(update_id=300, chat_id=-77, by=ALICE), deps)

    # the same button data replayed from the other group: its ledger, not yours
    actions = await dispatch(
        callback_update(update_id=301, chat_id=-77, data=older, message_id=555), deps
    )

    [ack] = actions  # ack only — never an edit leaking Japan Trip's stream
    assert type(ack).__name__ == "AnswerCallbackQuery"
    assert "doesn't match" in ack.text


async def test_garbled_tx_callback_data_gets_a_plain_ack(deps):
    await two_member_group(deps)

    for update_id, data in enumerate(
        ["v1:tx:junk", "v1:tx:1:n:abc:expense:1", "v1:tx:1:x:1:expense:1", "v1:tx:1:n:1:meal:1"],
        start=201,
    ):
        actions = await tap(deps, data, update_id=update_id)
        assert [type(a).__name__ for a in actions] == ["AnswerCallbackQuery"], data


async def test_transactions_are_sealed_to_the_active_ledger(deps):
    await two_member_group(deps)
    await say(deps, "/equal 60 EUR dinner", update_id=101, message_id=101)
    await say(deps, "/newledger Tokyo", update_id=102, message_id=102)

    [reply] = await say(deps, "/transactions", update_id=103, message_id=103)

    assert "Tokyo" in reply.text
    assert "No transactions yet" in reply.text  # Japan Trip's dinner never leaks in


class Seeder:
    """Direct-row setup for domain-level tests: created_at fully controlled."""

    def __init__(self, session):
        self.session = session
        self.ledger_id = 0
        self._payer_id = 0

    async def ledger(self) -> None:
        payer = DbUser(display_name="Alice")
        other = DbUser(display_name="Bob")
        self.session.add_all([payer, other])
        await self.session.flush()
        group = Group(platform_chat_id=-42, name="Japan Trip")
        self.session.add(group)
        await self.session.flush()
        ledger = Ledger(group_id=group.id, name="Japan Trip")
        self.session.add(ledger)
        await self.session.flush()
        self.ledger_id, self._payer_id, self._other_id = ledger.id, payer.id, other.id

    async def _action(self, kind: str) -> int:
        action = Action(
            ledger_id=self.ledger_id, actor_user_id=self._payer_id, kind=kind, intent_json={}
        )
        self.session.add(action)
        await self.session.flush()
        return action.id

    async def expense(self, created_at: datetime, description: str = "dinner") -> Expense:
        row = Expense(
            ledger_id=self.ledger_id,
            payer_id=self._payer_id,
            amount_minor=1000,
            currency="EUR",
            description=description,
            split_type="equal",
            source="command",
            created_by_user_id=self._payer_id,
            created_by_action_id=await self._action("add_expense"),
            created_at=created_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def settlement(self, created_at: datetime) -> Settlement:
        row = Settlement(
            ledger_id=self.ledger_id,
            from_user=self._payer_id,
            to_user=self._other_id,
            amount_minor=500,
            currency="EUR",
            created_by_action_id=await self._action("settle_up"),
            created_at=created_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row


SAME_INSTANT = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def at(minute: int) -> datetime:
    return datetime(2026, 7, 1, 12, minute, 0, tzinfo=UTC)


def cursor_of(tx) -> TransactionCursor:
    """Anchor on a returned TransactionRow, as the pager's callback will."""
    return TransactionCursor(created_at=tx.created_at, kind=tx.kind, id=tx.id)


def cursor_of_row(row, kind: str) -> TransactionCursor:
    """Anchor on a raw DB row, for pages that never rendered it."""
    return TransactionCursor(created_at=row.created_at, kind=kind, id=row.id)


def test_cursor_codec_round_trips_and_reads_naive_rows_as_utc():
    """SQLite hands DateTime(timezone=True) back naive-in-UTC; the codec must
    not reinterpret that wall-clock as local time (the tz-shift hazard)."""
    aware = TransactionCursor(
        created_at=datetime(2026, 7, 1, 12, 0, 0, 123456, tzinfo=UTC), kind="settlement", id=7
    )
    naive = TransactionCursor(
        created_at=datetime(2026, 7, 1, 12, 0, 0, 123456), kind="settlement", id=7
    )
    assert encode_cursor(naive) == encode_cursor(aware)
    assert decode_cursor(encode_cursor(naive)) == aware


def test_decode_cursor_keeps_its_value_error_contract_on_an_overflowing_epoch():
    """An epoch beyond datetime's range must surface as the documented
    ValueError, not leak timedelta's OverflowError to callers."""
    with pytest.raises(ValueError):
        decode_cursor(f"{10**30}:expense:1")


def test_pager_callback_data_stays_within_64_bytes_for_outsized_ids():
    """The worst-case grammar: 13-digit ledger and row ids, the longest kind
    ('settlement'), a year-2200 timestamp — still inside Telegram's budget."""
    edge = SettlementRow(
        id=10**13 - 1,
        amount_minor=1,
        currency="EUR",
        created_at=datetime(2200, 1, 1, tzinfo=UTC),
        created_by_action_id=1,
        from_name="Alice",
        to_name="Bob",
    )
    page = TransactionPage(rows=[edge], has_newer=True, has_older=True, total=10**13)

    markup = transactions_pager_keyboard(10**13 - 1, page)

    assert markup is not None
    [row] = markup["inline_keyboard"]
    assert len(row) == 2  # both directions anchored on the same outsized edge row
    for button in row:
        assert len(button["callback_data"].encode()) <= 64


def test_pager_keyboard_survives_an_empty_page_with_live_flags():
    """A cursor page can resolve to zero rows while rows remain on the other
    side (concurrent delete): the keyboard must not index into page.rows."""
    page = TransactionPage(rows=[], has_newer=True, has_older=True, total=3)
    assert transactions_pager_keyboard(1, page) is None


async def test_older_cursor_pages_forward_without_repeats_or_skips_despite_inserts(deps):
    async with deps.session_factory() as session, session.begin():
        seeder = Seeder(session)
        await seeder.ledger()
        e1 = await seeder.expense(at(1), "one")
        s2 = await seeder.settlement(at(2))
        e3 = await seeder.expense(at(3), "three")
        s4 = await seeder.settlement(at(4))
        e5 = await seeder.expense(at(5), "five")

        first = await list_transactions(session, seeder.ledger_id, limit=2)
        assert [(t.kind, t.id) for t in first.rows] == [("expense", e5.id), ("settlement", s4.id)]
        assert (first.has_newer, first.has_older) == (False, True)

        # a new transaction lands ABOVE the forward anchor: page 2 must not shift
        await seeder.expense(at(6), "six")

        second = await list_transactions(
            session, seeder.ledger_id, limit=2, cursor=cursor_of(first.rows[-1])
        )
        assert [(t.kind, t.id) for t in second.rows] == [("expense", e3.id), ("settlement", s2.id)]
        assert (second.has_newer, second.has_older) == (True, True)

        third = await list_transactions(
            session, seeder.ledger_id, limit=2, cursor=cursor_of(second.rows[-1])
        )
        assert [(t.kind, t.id) for t in third.rows] == [("expense", e1.id)]
        assert (third.has_newer, third.has_older) == (True, False)


async def test_newer_cursor_pages_back_in_stream_order(deps):
    async with deps.session_factory() as session, session.begin():
        seeder = Seeder(session)
        await seeder.ledger()
        await seeder.expense(at(1), "one")
        await seeder.settlement(at(2))
        e3 = await seeder.expense(at(3), "three")
        s4 = await seeder.settlement(at(4))
        e5 = await seeder.expense(at(5), "five")

        # ◀ from a page that started at e3: the two rows just above, newest first
        page = await list_transactions(
            session,
            seeder.ledger_id,
            limit=2,
            cursor=cursor_of_row(e3, "expense"),
            direction="newer",
        )
        assert [(t.kind, t.id) for t in page.rows] == [("expense", e5.id), ("settlement", s4.id)]
        assert (page.has_newer, page.has_older) == (False, True)


async def test_cursor_respects_the_kind_id_tiebreak_across_a_page_boundary(deps):
    async with deps.session_factory() as session, session.begin():
        seeder = Seeder(session)
        await seeder.ledger()
        e_a = await seeder.expense(SAME_INSTANT, "a")
        e_b = await seeder.expense(SAME_INSTANT, "b")
        s_c = await seeder.settlement(SAME_INSTANT)

        first = await list_transactions(session, seeder.ledger_id, limit=2)
        second = await list_transactions(
            session, seeder.ledger_id, limit=2, cursor=cursor_of(first.rows[-1])
        )

        walked = [(t.kind, t.id) for t in first.rows + second.rows]
        assert walked == [("expense", e_a.id), ("expense", e_b.id), ("settlement", s_c.id)]


async def test_identical_created_at_is_tiebroken_by_kind_then_id_deterministically(deps):
    async with deps.session_factory() as session, session.begin():
        seeder = Seeder(session)
        await seeder.ledger()
        # inserted in scrambled order, all at the very same instant
        e2 = await seeder.expense(SAME_INSTANT, "second expense")
        s1 = await seeder.settlement(SAME_INSTANT)
        e1 = await seeder.expense(SAME_INSTANT, "first expense")

        page = await list_transactions(session, seeder.ledger_id, limit=10)

    keys = [(tx.kind, tx.id) for tx in page.rows]
    # equal created_at: expenses before settlements, then ascending id
    assert keys == [("expense", e2.id), ("expense", e1.id), ("settlement", s1.id)]
    assert keys == sorted(keys)
