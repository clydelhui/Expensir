from expensir.core.outbound import (
    AnswerCallbackQuery,
    EditMessage,
    EditMessageReplyMarkup,
    SendMessage,
)
from expensir.transports.executor import execute


class FakeTelegramClient:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.markup_edits: list[tuple[int, int, dict]] = []
        self.answered: list[tuple[str, str | None]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None
    ) -> dict:
        self.edited.append((chat_id, message_id, text))
        return {"message_id": message_id}

    async def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: dict
    ) -> dict:
        self.markup_edits.append((chat_id, message_id, reply_markup))
        return {"message_id": message_id}

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
        self.answered.append((callback_query_id, text))
        return {}


async def test_executor_performs_send_message_actions_in_order():
    client = FakeTelegramClient()

    await execute(
        [SendMessage(chat_id=-42, text="first"), SendMessage(chat_id=-42, text="second")],
        client,
    )

    assert client.sent == [(-42, "first"), (-42, "second")]


async def test_executor_performs_edits_and_callback_answers():
    client = FakeTelegramClient()

    await execute(
        [
            AnswerCallbackQuery(callback_query_id="cbq-1", text="Undone."),
            EditMessage(chat_id=-42, message_id=555, text="new text"),
        ],
        client,
    )

    assert client.answered == [("cbq-1", "Undone.")]
    assert client.edited == [(-42, 555, "new text")]


async def test_executor_performs_markup_only_edits():
    client = FakeTelegramClient()
    markup = {"inline_keyboard": [[{"text": "↪️ Redo", "callback_data": "v1:redo:1"}]]}

    await execute(
        [EditMessageReplyMarkup(chat_id=-42, message_id=555, reply_markup=markup)], client
    )

    assert client.markup_edits == [(-42, 555, markup)]


class BrokenCosmeticsClient(FakeTelegramClient):
    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None
    ) -> dict:
        raise RuntimeError("message not found")

    async def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: dict
    ) -> dict:
        raise RuntimeError("message not found")

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
        raise RuntimeError("query is too old")


async def test_failed_edits_and_answers_are_best_effort_and_never_raise():
    """The DB transaction is the undo; the message sync is cosmetic (§9)."""
    client = BrokenCosmeticsClient()

    await execute(
        [
            AnswerCallbackQuery(callback_query_id="cbq-1", text="Undone."),
            EditMessage(chat_id=-42, message_id=555, text="new text"),
            EditMessageReplyMarkup(chat_id=-42, message_id=555, reply_markup={}),
            SendMessage(chat_id=-42, text="still delivered"),
        ],
        client,
    )

    assert client.sent == [(-42, "still delivered")]  # later actions still run
