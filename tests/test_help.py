from expensir.core.handler import _RUNNERS, HELP, dispatch
from tests.factories import bot_added_update, message_update, photo_update

TELEGRAM_MESSAGE_LIMIT = 4096


async def test_help_lists_commands_with_examples(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)

    actions = await dispatch(message_update(chat_id=-42, text="/help"), deps)

    [action] = actions
    assert action.kind == "send_message"
    assert action.chat_id == -42
    # one representative command per section
    for command in ("/equal", "/settle", "/transactions", "/setup", "/ledgers"):
        assert command in action.text
    # example-driven, not bare syntax notation
    assert "45 EUR" in action.text


async def test_help_fits_in_one_telegram_message():
    assert len(HELP) <= TELEGRAM_MESSAGE_LIMIT


async def test_help_covers_every_registered_command():
    # /help must not silently drift from the command registry: adding a runner
    # without documenting it fails here
    for command in _RUNNERS:
        assert f"/{command}" in HELP


async def test_help_as_photo_caption_is_claimed(deps):
    # a slash command typed as a caption is deterministic input — it runs the
    # command path, never the vision door (§13)
    await dispatch(bot_added_update(chat_id=-42), deps)

    actions = await dispatch(photo_update(update_id=3, chat_id=-42, caption="/help"), deps)

    [action] = actions
    assert action.kind == "send_message"
    assert "/equal" in action.text


async def test_help_addressed_to_this_bot_replies(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)

    actions = await dispatch(
        message_update(update_id=3, chat_id=-42, text="/help@expensir_bot"), deps
    )

    assert len(actions) == 1
    assert "/equal" in actions[0].text


async def test_help_addressed_to_another_bot_is_ignored(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)

    assert (
        await dispatch(message_update(update_id=3, chat_id=-42, text="/help@other_bot"), deps) == []
    )
