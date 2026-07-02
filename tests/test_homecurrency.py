from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Action, Group
from tests.factories import bot_added_update, message_update


async def read_group(deps, chat_id: int) -> Group:
    async with deps.session_factory() as session:
        return (
            await session.execute(select(Group).where(Group.platform_chat_id == chat_id))
        ).scalar_one()


async def test_homecurrency_sets_the_group_home_currency(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)

    [reply] = await dispatch(
        message_update(update_id=3, chat_id=-42, text="/homecurrency eur"), deps
    )

    assert "EUR" in reply.text
    assert (await read_group(deps, -42)).home_currency == "EUR"


async def test_homecurrency_appends_one_action_row_with_the_prior_value(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)

    await dispatch(message_update(update_id=3, chat_id=-42, text="/homecurrency EUR"), deps)
    await dispatch(message_update(update_id=4, chat_id=-42, text="/homecurrency SGD"), deps)

    async with deps.session_factory() as session:
        actions = (await session.execute(select(Action).order_by(Action.id))).scalars().all()
    assert [a.kind for a in actions] == ["set_home_currency", "set_home_currency"]
    first, second = actions
    # undo (slice 7) restores from before_image; the log must capture the prior value
    assert first.before_image == {"home_currency": None}
    assert second.before_image == {"home_currency": "EUR"}
    assert first.intent_json["currency"] == "EUR"
    assert first.actor_user_id is not None
    assert first.ledger_id == (await read_group(deps, -42)).active_ledger_id


async def test_homecurrency_with_bad_input_replies_usage_and_sets_nothing(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)

    [reply] = await dispatch(
        message_update(update_id=3, chat_id=-42, text="/homecurrency dollars"), deps
    )

    assert "Usage" in reply.text
    group = await read_group(deps, -42)
    assert group.home_currency is None
    async with deps.session_factory() as session:
        assert (await session.execute(select(Action))).scalars().all() == []
