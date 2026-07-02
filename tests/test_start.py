from expensir.core.handler import dispatch
from tests.factories import bot_added_update, message_update


async def test_start_replies_naming_the_active_ledger(deps):
    await dispatch(bot_added_update(chat_id=-42, title="Japan Trip"), deps)

    actions = await dispatch(message_update(chat_id=-42, text="/start"), deps)

    [action] = actions
    assert action.kind == "send_message"
    assert action.chat_id == -42
    assert "Japan Trip" in action.text


async def test_start_addressed_to_this_bot_replies(deps):
    await dispatch(bot_added_update(chat_id=-42, title="Japan Trip"), deps)

    actions = await dispatch(
        message_update(update_id=3, chat_id=-42, text="/start@expensir_bot"), deps
    )

    assert len(actions) == 1
    assert "Japan Trip" in actions[0].text


async def test_start_addressed_to_another_bot_is_ignored(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)

    assert (
        await dispatch(message_update(update_id=3, chat_id=-42, text="/start@other_bot"), deps)
        == []
    )


async def test_unrecognized_updates_produce_no_actions(deps):
    assert await dispatch({"update_id": 5}, deps) == []
    assert await dispatch({"update_id": 6, "poll_answer": {"poll_id": "1"}}, deps) == []
    # plain group chatter never gets a reply (§0.6)
    assert await dispatch(message_update(update_id=7, text="what a lovely day"), deps) == []
