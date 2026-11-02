from aiogram.types import Message

from Handlers.menus import pages_keyboard
from Services.users import UserService
from Utils.telegram_formatter import split_pages, telegram_html
from state import BotState, ConversationKey, ConversationSnapshot, PaginationEntry


def conversation_key(message: Message) -> ConversationKey:
    return message.chat.id, message.from_user.id


async def send_paginated(
    message: Message,
    text: str,
    state: BotState,
    conversation: ConversationSnapshot,
    users: UserService | None = None,
) -> bool:
    if users is not None:
        await users.require_access(conversation.key[1])
    # Last check before beginning the Telegram send operation.
    if not state.is_current(conversation):
        return False
    pages = [telegram_html(page) for page in split_pages(text)]
    sent = await message.answer(
        pages[0],
        parse_mode="HTML",
        reply_markup=pages_keyboard(0, len(pages)),
    )
    # If reset happened while Telegram was accepting the message, remove the stale
    # answer best-effort and never attach it to the new generation's pagination.
    if not state.is_current(conversation):
        try:
            await sent.delete()
        except Exception:
            pass
        return False
    if len(pages) > 1:
        state.response_pages[(sent.chat.id, sent.message_id)] = PaginationEntry(
            pages=pages,
            conversation=conversation,
        )
    return True
