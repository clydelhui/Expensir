import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from expensir.core.handler import Deps, dispatch
from expensir.db.models import Base, Group, Ledger
from tests.factories import bot_added_update, message_update


async def test_adding_bot_to_group_posts_welcome_with_required_content(deps):
    actions = await dispatch(bot_added_update(chat_id=-42, title="Japan Trip"), deps)

    [action] = actions
    assert action.kind == "send_message"
    assert action.chat_id == -42
    # §11 onboarding: the welcome must (a) ask for the home currency, (b) mention the
    # per-ledger logging currency, (c) explain /setup and the bare-username limitation,
    # (d) note that photos/NL need an @mention or reply.
    assert "/homecurrency" in action.text
    assert "/currency" in action.text
    assert "/setup" in action.text
    assert "@username" in action.text
    assert "mention" in action.text.lower()


async def test_adding_bot_creates_group_and_first_ledger_named_after_group(deps):
    await dispatch(bot_added_update(chat_id=-42, title="Japan Trip"), deps)

    async with deps.session_factory() as session:
        group = (
            await session.execute(select(Group).where(Group.platform_chat_id == -42))
        ).scalar_one()
        assert group.name == "Japan Trip"
        assert group.home_currency is None

        ledger = (
            await session.execute(select(Ledger).where(Ledger.group_id == group.id))
        ).scalar_one()
        assert ledger.name == "Japan Trip"
        assert ledger.status == "open"
        assert ledger.logging_currency is None
        assert group.active_ledger_id == ledger.id


async def test_first_ledger_falls_back_to_general_when_group_has_no_title(deps):
    await dispatch(bot_added_update(chat_id=-42, title=None), deps)

    async with deps.session_factory() as session:
        ledger = (await session.execute(select(Ledger))).scalar_one()
        assert ledger.name == "General"


async def test_promotion_to_admin_is_not_an_add_and_posts_no_welcome(deps):
    await dispatch(bot_added_update(update_id=1, chat_id=-42), deps)

    promotion = bot_added_update(
        update_id=2, chat_id=-42, old_status="member", new_status="administrator"
    )
    assert await dispatch(promotion, deps) == []


async def test_added_with_restrictions_still_onboards(deps):
    # ChatMemberRestricted with is_member=True means the bot IS in the group
    update = bot_added_update(chat_id=-42, new_status="restricted", new_is_member=True)

    actions = await dispatch(update, deps)

    assert len(actions) == 1
    async with deps.session_factory() as session:
        assert (await session.execute(select(Group))).scalar_one().platform_chat_id == -42


async def test_readded_after_restriction_while_out_gets_the_welcome(deps):
    update = bot_added_update(
        chat_id=-42, old_status="restricted", old_is_member=False, new_status="member"
    )

    assert len(await dispatch(update, deps)) == 1


async def test_anonymous_admin_service_account_is_never_registered(deps):
    group_anonymous_bot = {
        "id": 1087968824,
        "is_bot": True,
        "first_name": "Group",
        "username": "GroupAnonymousBot",
    }
    await dispatch(bot_added_update(chat_id=-42, by=group_anonymous_bot), deps)

    async with deps.session_factory() as session:
        from expensir.db.models import User

        assert (await session.execute(select(User))).scalars().all() == []


async def test_adding_bot_twice_does_not_duplicate_group_or_ledger(deps):
    await dispatch(bot_added_update(update_id=1, chat_id=-42), deps)
    await dispatch(bot_added_update(update_id=2, chat_id=-42), deps)

    async with deps.session_factory() as session:
        group = (await session.execute(select(Group))).scalar_one()
        ledger = (await session.execute(select(Ledger))).scalar_one()
        assert group.active_ledger_id == ledger.id


async def test_concurrent_first_contact_updates_both_succeed(tmp_path):
    """Telegram delivers my_chat_member AND a service message concurrently on bot-add;
    the get-or-create paths must survive losing the insert race (no 500s)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/race.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    deps = Deps(session_factory=async_sessionmaker(engine, expire_on_commit=False))

    added = bot_added_update(update_id=1, chat_id=-42)
    start = message_update(update_id=2, chat_id=-42, text="/start")
    results = await asyncio.gather(dispatch(added, deps), dispatch(start, deps))

    assert all(isinstance(r, list) for r in results)
    async with deps.session_factory() as session:
        (await session.execute(select(Group))).scalar_one()
        (await session.execute(select(Ledger))).scalar_one()
    await engine.dispose()
