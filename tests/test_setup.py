"""/setup — registering pre-existing members (§11, issue #12).

Registration via /setup is permanent: it appends an action row but never an
Undo affordance.
"""

from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Action, Expense, GroupMember, Identity
from tests.factories import (
    bot_added_update,
    callback_update,
    left_member_update,
    message_update,
    user,
)

ALICE = user(1001, "Alice", "alice")
CAROL = user(1003, "Carol", "carol")


async def setup_group(deps, chat_id: int = -42) -> None:
    await dispatch(bot_added_update(chat_id=chat_id, by=ALICE), deps)


async def read_setup_actions(deps) -> list[Action]:
    async with deps.session_factory() as session:
        return list((await session.execute(select(Action).where(Action.kind == "setup"))).scalars())


async def read_platform_ids(deps) -> set[int]:
    async with deps.session_factory() as session:
        identities = (await session.execute(select(Identity))).scalars()
        return {i.platform_user_id for i in identities}


async def test_reply_setup_registers_the_replied_to_member(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(
            update_id=2,
            chat_id=-42,
            text="/setup",
            from_user=ALICE,
            reply_to_message_id=77,
            reply_to_from=CAROL,
        ),
        deps,
    )

    assert await read_platform_ids(deps) == {1001, 1003}
    async with deps.session_factory() as session:
        memberships = (await session.execute(select(GroupMember))).scalars().all()
        assert len(memberships) == 2
        action = (await session.execute(select(Action).where(Action.kind == "setup"))).scalar_one()
        assert action.ledger_id is None  # registration is group-scoped, not ledger activity

    assert reply.kind == "send_message"
    assert "Carol" in reply.text
    assert reply.reply_markup is None  # registration is permanent: no Undo button (§8)


async def test_text_mention_registers_and_bare_username_is_rejected(deps):
    """A text_mention embeds the user id and registers; a bare @username can't be
    resolved by the Bot API, so that entry is individually rejected with guidance."""
    await setup_group(deps)

    text = "/setup Dave @carol"
    dave = {"id": 1004, "is_bot": False, "first_name": "Dave"}  # no username: typical
    entities = [
        {"type": "bot_command", "offset": 0, "length": 6},
        {"type": "text_mention", "offset": 7, "length": 4, "user": dave},
        {"type": "mention", "offset": 12, "length": 6},
    ]

    [reply] = await dispatch(
        message_update(update_id=2, chat_id=-42, text=text, from_user=ALICE, entities=entities),
        deps,
    )

    assert await read_platform_ids(deps) == {1001, 1004}  # Dave in, @carol not
    assert "Dave" in reply.text
    assert "@carol" in reply.text  # the rejected entry is named, with guidance
    assert "reply" in reply.text.lower()  # ...pointing at the working alternatives
    assert reply.reply_markup is None


async def test_bare_setup_replies_with_usage_and_writes_no_action(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=2, chat_id=-42, text="/setup", from_user=ALICE), deps
    )

    assert "reply" in reply.text.lower()  # how to use it: reply to their message...
    assert "mention" in reply.text.lower()  # ...or tag them directly
    assert await read_setup_actions(deps) == []  # nothing registered -> nothing audited


async def test_setup_replying_to_a_bot_message_rejects_the_entry(deps):
    """People reply to the bot constantly — the likeliest /setup misfire. No ghosts,
    no bot members (§11)."""
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(
            update_id=2,
            chat_id=-42,
            text="/setup",
            from_user=ALICE,
            reply_to_message_id=55,  # reply_to_from defaults to the bot itself
        ),
        deps,
    )

    assert "bot" in reply.text.lower()  # the entry is rejected as a bot, with a reason
    assert await read_platform_ids(deps) == {1001}  # only Alice; no bot rows
    assert await read_setup_actions(deps) == []


async def test_setup_on_a_current_member_says_already_and_audits_nothing(deps):
    await setup_group(deps)
    bob = user(1002, "Bob", "bob")
    await dispatch(message_update(update_id=2, chat_id=-42, text="hello", from_user=bob), deps)

    [reply] = await dispatch(
        message_update(
            update_id=3,
            chat_id=-42,
            text="/setup",
            from_user=ALICE,
            reply_to_message_id=60,
            reply_to_from=bob,
        ),
        deps,
    )

    assert "Bob" in reply.text
    assert "already" in reply.text.lower()
    assert await read_setup_actions(deps) == []  # nothing created -> nothing audited
    async with deps.session_factory() as session:
        memberships = (await session.execute(select(GroupMember))).scalars().all()
        assert len(memberships) == 2  # Alice + Bob, no duplicate rows


async def test_expense_fails_on_unknown_then_commits_after_reply_setup(deps):
    """Issue #12 AC: the rejection is final — /setup then a manual re-send commits."""
    await setup_group(deps)
    await dispatch(
        message_update(update_id=2, chat_id=-42, text="/homecurrency EUR", from_user=ALICE),
        deps,
    )

    [rejected] = await dispatch(
        message_update(
            update_id=3, chat_id=-42, text="/equal 60 dinner @alice @dave", from_user=ALICE
        ),
        deps,
    )
    assert "@dave" in rejected.text
    assert "/setup" in rejected.text  # the rejection teaches the fix
    async with deps.session_factory() as session:
        assert (await session.execute(select(Expense))).scalars().all() == []

    dave = user(1004, "Dave", "dave")
    await dispatch(
        message_update(
            update_id=4,
            chat_id=-42,
            text="/setup",
            from_user=ALICE,
            reply_to_message_id=70,
            reply_to_from=dave,
        ),
        deps,
    )

    [committed] = await dispatch(
        message_update(
            update_id=5, chat_id=-42, text="/equal 60 dinner @alice @dave", from_user=ALICE
        ),
        deps,
    )
    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.amount_minor == 6000
    assert "dinner" in committed.text


async def test_setup_naming_a_departed_member_does_not_reactivate(deps):
    """Reactivation is strictly self-triggered (§11): no third party's /setup brings
    a departed member back — they must re-join or interact themselves."""
    await setup_group(deps)
    bob = user(1002, "Bob", "bob")
    await dispatch(message_update(update_id=2, chat_id=-42, text="hello", from_user=bob), deps)
    await dispatch(left_member_update(update_id=3, chat_id=-42, member=bob), deps)

    [reply] = await dispatch(
        message_update(
            update_id=4,
            chat_id=-42,
            text="/setup",
            from_user=ALICE,
            reply_to_message_id=60,
            reply_to_from=bob,
        ),
        deps,
    )

    assert "Bob" in reply.text
    assert "left" in reply.text.lower()  # the entry names the departure, not an error
    assert await read_setup_actions(deps) == []  # nothing changed -> nothing audited
    async with deps.session_factory() as session:
        membership = (
            await session.execute(
                select(GroupMember)
                .join(Identity, Identity.user_id == GroupMember.user_id)
                .where(Identity.platform_user_id == 1002)
            )
        ).scalar_one()
        assert membership.left_at is not None  # still departed


async def test_setup_snapshot_never_overwrites_a_fresher_identity(deps):
    """A reply target's User object is as-of the original message (§11): it may seed
    a new registration, but a live interaction's data always wins over it."""
    await setup_group(deps)
    bob = user(1002, "Bob", "bob")
    await dispatch(message_update(update_id=2, chat_id=-42, text="hello", from_user=bob), deps)
    renamed = user(1002, "Bobby", "bobby")
    await dispatch(
        message_update(update_id=3, chat_id=-42, text="it's bobby now", from_user=renamed),
        deps,
    )

    # Alice replies /setup to Bob's OLD message — its snapshot still says @bob
    await dispatch(
        message_update(
            update_id=4,
            chat_id=-42,
            text="/setup",
            from_user=ALICE,
            reply_to_message_id=11,
            reply_to_from=bob,
        ),
        deps,
    )

    async with deps.session_factory() as session:
        identity = (
            await session.execute(select(Identity).where(Identity.platform_user_id == 1002))
        ).scalar_one()
        assert identity.username == "bobby"  # the stale snapshot did not win


async def test_undo_callback_on_a_setup_action_is_refused(deps):
    """Registration is permanent (§8): the action row exists for audit, but no
    callback can reverse it."""
    await setup_group(deps)
    await dispatch(
        message_update(
            update_id=2,
            chat_id=-42,
            text="/setup",
            from_user=ALICE,
            reply_to_message_id=77,
            reply_to_from=CAROL,
        ),
        deps,
    )
    [action] = await read_setup_actions(deps)

    [ack] = await dispatch(
        callback_update(update_id=3, chat_id=-42, data=f"v1:undo:{action.id}", from_user=ALICE),
        deps,
    )

    assert "permanent" in ack.text
    assert await read_platform_ids(deps) == {1001, 1003}  # Carol is still a member
