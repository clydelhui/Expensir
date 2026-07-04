"""Synthetic Telegram update dicts for driving the dispatch seam."""

BOT_USER = {"id": 999999, "is_bot": True, "first_name": "Expensir", "username": "expensir_bot"}


def user(user_id: int = 1001, first_name: str = "Alice", username: str | None = "alice") -> dict:
    u = {"id": user_id, "is_bot": False, "first_name": first_name}
    if username is not None:
        u["username"] = username
    return u


def group_chat(chat_id: int = -100500, title: str | None = "Japan Trip") -> dict:
    chat = {"id": chat_id, "type": "group"}
    if title is not None:
        chat["title"] = title
    return chat


def bot_added_update(
    update_id: int = 1,
    chat_id: int = -100500,
    title: str | None = "Japan Trip",
    by: dict | None = None,
    old_status: str = "left",
    new_status: str = "member",
    old_is_member: bool | None = None,
    new_is_member: bool | None = None,
) -> dict:
    old = {"user": BOT_USER, "status": old_status}
    if old_is_member is not None:
        old["is_member"] = old_is_member
    new = {"user": BOT_USER, "status": new_status}
    if new_is_member is not None:
        new["is_member"] = new_is_member
    return {
        "update_id": update_id,
        "my_chat_member": {
            "chat": group_chat(chat_id, title),
            "from": by or user(),
            "date": 1751400000,
            "old_chat_member": old,
            "new_chat_member": new,
        },
    }


def callback_update(
    update_id: int = 3,
    chat_id: int = -100500,
    data: str = "v1:undo:1",
    from_user: dict | None = None,
    message_id: int = 555,
    message_text: str | None = "📒 Japan Trip • #1 dinner — EUR 60.00 paid by Alice.",
    callback_query_id: str = "cbq-1",
) -> dict:
    # message_text=None models Telegram's InaccessibleMessage (callback on a very
    # old message): chat + message_id survive, text does not, and date is 0
    message = {"message_id": message_id, "chat": group_chat(chat_id), "date": 0}
    if message_text is not None:
        message["date"] = 1751400000
        message["text"] = message_text
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_query_id,
            "from": from_user or user(),
            "chat_instance": "ci-1",
            "data": data,
            "message": message,
        },
    }


def message_update(
    update_id: int = 2,
    chat_id: int = -100500,
    title: str | None = "Japan Trip",
    text: str = "/start",
    from_user: dict | None = None,
    message_id: int = 10,
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "chat": group_chat(chat_id, title),
            "from": from_user or user(),
            "date": 1751400000,
            "text": text,
        },
    }
