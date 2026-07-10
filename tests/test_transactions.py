"""/transactions page 1 (#24, ADR-0012): the merged history read + shared primitives."""

from datetime import UTC, datetime

from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Action, Expense, Group, Ledger, Settlement
from expensir.db.models import User as DbUser
from expensir.domain.transactions import (
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


async def test_more_than_ten_transactions_render_an_older_pager_that_safely_acks(deps):
    await two_member_group(deps)
    for n in range(11):
        await say(deps, f"/equal 10 EUR item{n}", update_id=101 + n, message_id=101 + n)

    [reply] = await say(deps, "/transactions", update_id=200, message_id=200)

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

    # this slice ships no cursor handler: a tap must ack without side effects
    actions = await dispatch(
        callback_update(update_id=201, chat_id=-42, data=older["callback_data"]), deps
    )
    assert [type(a).__name__ for a in actions] == ["AnswerCallbackQuery"]


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
