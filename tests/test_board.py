"""The pinned board (§13, issue #9): rendering, create+pin once, edit-in-place."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from expensir.core.handler import dispatch
from expensir.db.models import Action, Expense, Ledger
from expensir.format.board import board_text
from expensir.transports.executor import execute
from tests.factories import bot_added_update, callback_update, message_update, user

ALICE = user(1001, "Alice", "alice")
BOB = user(1002, "Bob", "bob")


class FakeBoardMessenger:
    """The one call board creation makes inside the locked transaction (ADR-0003):
    the send. Pinning rides a post-commit outbound, so it is not in here."""

    def __init__(self, send_error: Exception | None = None, fixed_message_id: int | None = None):
        self.sent: list[dict] = []
        self.send_error = send_error
        self.fixed_message_id = fixed_message_id  # force a board-id collision

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append({"chat_id": chat_id, "text": text})
        return {"message_id": self.fixed_message_id or 900 + len(self.sent)}


async def setup_group(deps, chat_id: int = -42) -> None:
    await dispatch(bot_added_update(chat_id=chat_id, by=ALICE), deps)
    await dispatch(message_update(update_id=90, chat_id=chat_id, text="/homecurrency EUR"), deps)
    # Bob becomes registered by interacting once (§11)
    await dispatch(message_update(update_id=91, chat_id=chat_id, text="hello", from_user=BOB), deps)


async def read_action(deps, kind: str) -> Action:
    async with deps.session_factory() as session:
        return (await session.execute(select(Action).where(Action.kind == kind))).scalar_one()


async def read_ledgers(deps) -> list[Ledger]:
    async with deps.session_factory() as session:
        return list((await session.execute(select(Ledger).order_by(Ledger.id))).scalars())


async def test_first_mutation_creates_the_board_and_requests_its_pin(deps):
    deps.client = messenger = FakeBoardMessenger()
    await dispatch(bot_added_update(chat_id=-42, by=ALICE), deps)

    outbound = await dispatch(  # the ledger's first mutation
        message_update(update_id=90, chat_id=-42, text="/homecurrency EUR"), deps
    )

    [board] = messenger.sent
    assert board["chat_id"] == -42
    assert board["text"] == "📒 Japan Trip • Board\nAll settled up."
    [ledger] = await read_ledgers(deps)
    assert ledger.board_chat_id == -42
    assert ledger.board_message_id == 901
    # the pin itself is post-commit and best-effort: it rides the outbounds
    [pin] = [a for a in outbound if a.kind == "pin_chat_message"]
    assert (pin.chat_id, pin.message_id) == (-42, 901)
    assert "couldn't pin" in pin.warn_text


async def test_later_mutations_edit_the_board_in_place_with_post_write_balances(deps):
    deps.client = messenger = FakeBoardMessenger()
    await setup_group(deps)

    outbound = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )

    assert len(messenger.sent) == 1  # created exactly once, never re-sent
    [board_edit] = [a for a in outbound if a.kind == "edit_message"]
    assert (board_edit.chat_id, board_edit.message_id) == (-42, 901)
    assert board_edit.text == "📒 Japan Trip • Board\nBob → Alice EUR 30.00"


async def test_board_simplifies_each_currency_independently(deps):
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )

    outbound = await dispatch(
        message_update(update_id=6, chat_id=-42, text="/exact 600 JPY snacks @alice=200 @bob=400"),
        deps,
    )

    [board_edit] = [a for a in outbound if a.kind == "edit_message"]
    assert board_edit.text == ("📒 Japan Trip • Board\nBob → Alice EUR 30.00\nBob → Alice JPY 400")


async def test_newledger_creates_and_pins_its_own_board(deps):
    """Deferred from slice 7: /newledger's board exists from birth (§13)."""
    deps.client = messenger = FakeBoardMessenger()
    await setup_group(deps)

    outbound = await dispatch(
        message_update(update_id=6, chat_id=-42, text="/newledger Tokyo"), deps
    )

    assert len(messenger.sent) == 2  # one board per ledger
    assert messenger.sent[1]["text"] == "📒 Tokyo • Board\nAll settled up."
    _, tokyo = await read_ledgers(deps)
    assert (tokyo.board_chat_id, tokyo.board_message_id) == (-42, 902)
    [pin] = [a for a in outbound if a.kind == "pin_chat_message"]
    assert (pin.chat_id, pin.message_id) == (-42, 902)


async def test_reads_and_rejections_leave_the_board_alone(deps):
    deps.client = messenger = FakeBoardMessenger()
    await setup_group(deps)

    for text in ("/balance", "/ledgers", "/equal nonsense"):
        outbound = await dispatch(
            message_update(update_id=50 + hash(text) % 40, chat_id=-42, text=text), deps
        )
        assert [a.kind for a in outbound] == ["send_message"], text

    assert len(messenger.sent) == 1  # just the board itself, from setup


async def test_deleting_an_expense_re_renders_the_board_without_it(deps):
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )

    outbound = await dispatch(message_update(update_id=6, chat_id=-42, text="/delete 1"), deps)

    [board_edit] = [a for a in outbound if a.kind == "edit_message"]
    assert board_edit.text == "📒 Japan Trip • Board\nAll settled up."


def board_edits(outbound, board_message_id: int = 901) -> list:
    """The board's edits among the outbounds (the undo path also edits the result message)."""
    return [a for a in outbound if a.kind == "edit_message" and a.message_id == board_message_id]


async def test_undo_and_redo_re_render_the_board(deps):
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )
    action = await read_action(deps, "add_expense")

    undone = await dispatch(
        callback_update(update_id=7, chat_id=-42, data=f"v1:undo:{action.id}"), deps
    )
    [board_edit] = board_edits(undone)
    assert board_edit.text == "📒 Japan Trip • Board\nAll settled up."

    redone = await dispatch(
        callback_update(update_id=8, chat_id=-42, data=f"v1:redo:{action.id}"), deps
    )
    [board_edit] = board_edits(redone)
    assert board_edit.text == "📒 Japan Trip • Board\nBob → Alice EUR 30.00"


async def test_a_stale_tap_does_not_touch_the_board(deps):
    """'Already undone' toggles nothing (§9), so there is nothing to re-render."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )
    action = await read_action(deps, "add_expense")
    await dispatch(callback_update(update_id=7, chat_id=-42, data=f"v1:undo:{action.id}"), deps)

    stale = await dispatch(
        callback_update(update_id=8, chat_id=-42, data=f"v1:undo:{action.id}"), deps
    )

    assert board_edits(stale) == []


class PinRefusingClient:
    """A client whose bot is not a group admin: pins are refused, everything else works."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text})
        return {"message_id": 700 + len(self.sent)}

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        return {"message_id": message_id}

    async def pin_chat_message(self, chat_id, message_id):
        raise RuntimeError("not enough rights to manage pinned messages")


async def test_without_admin_rights_the_board_posts_unpinned_with_one_warning(deps):
    deps.client = messenger = FakeBoardMessenger()
    await dispatch(bot_added_update(chat_id=-42, by=ALICE), deps)
    outbound = await dispatch(
        message_update(update_id=90, chat_id=-42, text="/homecurrency EUR"), deps
    )

    no_admin = PinRefusingClient()
    await execute(outbound, no_admin, session_factory=deps.session_factory)

    # the pin failed post-commit: the board stays (unpinned) and ONE warning is sent
    [ledger] = await read_ledgers(deps)
    assert ledger.board_message_id == 901
    warnings = [s for s in no_admin.sent if "couldn't pin" in s["text"]]
    assert len(warnings) == 1
    assert warnings[0]["chat_id"] == -42

    # later mutations edit the board in place: no pin request, so no second warning
    await dispatch(message_update(update_id=91, chat_id=-42, text="hello", from_user=BOB), deps)
    outbound = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )
    assert [a for a in outbound if a.kind == "pin_chat_message"] == []
    assert len(board_edits(outbound)) == 1
    assert len(messenger.sent) == 1
    before = len(no_admin.sent)
    await execute(outbound, no_admin, session_factory=deps.session_factory)
    assert [s for s in no_admin.sent[before:] if "couldn't pin" in s["text"]] == []


async def test_a_failed_board_send_never_loses_the_write_and_creation_retries(deps):
    deps.client = messenger = FakeBoardMessenger(send_error=RuntimeError("network down"))
    await setup_group(deps)

    outbound = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )

    # the write committed even though every board create so far failed
    assert any(a.kind == "send_message" and "dinner" in a.text for a in outbound)
    [ledger] = await read_ledgers(deps)
    assert ledger.board_message_id is None

    # the next mutation retries creation and the board arrives with current balances
    messenger.send_error = None
    await dispatch(
        message_update(update_id=6, chat_id=-42, text="/equal 20 taxi @alice @bob"), deps
    )
    [board] = messenger.sent
    assert board["text"] == "📒 Japan Trip • Board\nBob → Alice EUR 40.00"
    [ledger] = await read_ledgers(deps)
    assert ledger.board_message_id == 901


class FailingEditClient:
    """Sends succeed; every edit blows up — the board message is gone or Telegram is down."""

    async def send_message(self, chat_id, text, reply_markup=None):
        return {"message_id": 777}

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        raise RuntimeError("message to edit not found")


async def test_a_failed_board_edit_never_loses_the_committed_write(deps):
    deps.client = FakeBoardMessenger()
    await setup_group(deps)

    outbound = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )
    assert len(board_edits(outbound)) == 1
    await execute(outbound, FailingEditClient(), session_factory=deps.session_factory)

    async with deps.session_factory() as session:
        [expense] = (await session.execute(select(Expense))).scalars().all()
    assert expense.deleted_at is None  # the edit failure cost only the board sync


async def test_two_groups_boards_may_share_a_message_id_but_one_chat_may_not(deps):
    """The create-once guard is composite (§5, #21): message ids are only unique per chat."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps, chat_id=-42)
    await setup_group(deps, chat_id=-43)  # the same fake message ids in a different chat: fine

    async with deps.session_factory() as session:
        boards = {
            (ledger.board_chat_id, ledger.board_message_id)
            for ledger in (await session.execute(select(Ledger))).scalars()
        }
    assert boards == {(-42, 901), (-43, 902)}

    async with deps.session_factory() as session, session.begin():
        [taken] = (
            await session.execute(select(Ledger).where(Ledger.board_chat_id == -42))
        ).scalars()
        clone = Ledger(
            group_id=taken.group_id,
            name="imposter",
            board_chat_id=taken.board_chat_id,
            board_message_id=taken.board_message_id,
        )
        session.add(clone)
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_a_board_id_collision_is_swallowed_and_never_rolls_back_the_write(deps):
    """The composite-unique backstop (§5, ADR-0003) fires inside a savepoint: the
    mutation still commits; only the board ids are lost (creation retries later)."""
    deps.client = FakeBoardMessenger(fixed_message_id=901)
    await setup_group(deps)  # the first ledger claims (-42, 901)

    outbound = await dispatch(  # Tokyo's board send returns 901 again -> collision
        message_update(update_id=6, chat_id=-42, text="/newledger Tokyo"), deps
    )

    japan, tokyo = await read_ledgers(deps)
    assert tokyo.name == "Tokyo"
    assert tokyo.status == "open"  # the mutation itself committed untouched
    assert (japan.board_chat_id, japan.board_message_id) == (-42, 901)
    assert tokyo.board_message_id is None  # the collision cost only the board ids
    assert any(a.kind == "send_message" and "active ledger" in a.text for a in outbound)
    assert [a for a in outbound if a.kind == "pin_chat_message"] == []  # nothing to pin


async def test_archiving_a_boardless_ledger_does_not_mint_it_a_board(deps):
    """A board pins forever (§13 never-delete): never create one for a retiring ledger."""
    await setup_group(deps)  # deps.client is None: the first ledger has no board
    deps.client = messenger = FakeBoardMessenger()
    await dispatch(message_update(update_id=6, chat_id=-42, text="/newledger Tokyo"), deps)
    assert len(messenger.sent) == 1  # Tokyo's board only

    outbound = await dispatch(
        message_update(update_id=7, chat_id=-42, text="/archive Japan Trip"), deps
    )

    assert any(a.kind == "send_message" and "Archived" in a.text for a in outbound)
    assert len(messenger.sent) == 1  # no board minted for the dying ledger
    japan, _ = await read_ledgers(deps)
    assert japan.status == "archived"
    assert japan.board_message_id is None


async def test_board_sync_targets_the_mutations_ledger_not_the_active_one(deps):
    """§13: every mutation re-renders ITS ledger's board — routing keys on the
    action's ledger, which archive/unarchive/undo can point away from the active one."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)  # Japan Trip board = 901
    await dispatch(message_update(update_id=6, chat_id=-42, text="/newledger Tokyo"), deps)
    # Tokyo (board 902) is now active; mutate Japan Trip from behind it
    outbound = await dispatch(
        message_update(update_id=7, chat_id=-42, text="/archive Japan Trip"), deps
    )

    assert len(board_edits(outbound, 901)) == 1  # Japan's board re-rendered
    assert board_edits(outbound, 902) == []  # Tokyo's untouched

    action = await read_action(deps, "archive_ledger")
    undone = await dispatch(
        callback_update(update_id=8, chat_id=-42, data=f"v1:undo:{action.id}"), deps
    )
    assert len(board_edits(undone, 901)) == 1
    assert board_edits(undone, 902) == []


async def test_undoing_newledger_after_a_failed_board_send_does_not_mint_one(deps):
    deps.client = messenger = FakeBoardMessenger()
    await setup_group(deps)
    messenger.send_error = RuntimeError("network down")
    await dispatch(message_update(update_id=6, chat_id=-42, text="/newledger Tokyo"), deps)
    messenger.send_error = None
    action = await read_action(deps, "new_ledger")

    await dispatch(callback_update(update_id=7, chat_id=-42, data=f"v1:undo:{action.id}"), deps)

    _, tokyo = await read_ledgers(deps)
    assert tokyo.status == "archived"  # the undone shell
    assert tokyo.board_message_id is None
    assert len(messenger.sent) == 1  # only the first ledger's board, from setup


def test_board_lists_suggested_transfers_per_currency():
    text = board_text(
        ledger_name="Japan Trip",
        transfers=[
            ("Alice", "Bob", 6000, "EUR"),
            ("Carol", "Bob", 500, "JPY"),
        ],
    )

    assert text == ("📒 Japan Trip • Board\nAlice → Bob EUR 60.00\nCarol → Bob JPY 500")


def test_settled_board_says_so():
    assert board_text(ledger_name="Japan Trip", transfers=[]) == (
        "📒 Japan Trip • Board\nAll settled up."
    )
