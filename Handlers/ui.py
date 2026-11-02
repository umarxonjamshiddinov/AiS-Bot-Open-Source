from datetime import datetime
from html import escape

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def keyboard(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows if row
    ])


def date_label(value):
    return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M UTC") if value else "—"


def user_label(user):
    name = " ".join(filter(None, (user.get("first_name"), user.get("last_name"))))
    return ("@" + user["username"] if user.get("username") else name or str(user.get("telegram_user_id", "Пользователь")))[:48]


def safe(value):
    return escape(str(value)) if value is not None else "—"


def limit_label(value):
    return "без лимита" if value is None else f"{value} / сутки"


def pager(page, route):
    buttons = []
    if page.page > 0:
        buttons.append(("‹ Назад", route(page.page - 1)))
    buttons.append((f"{page.page + 1} / {page.pages}", route(page.page)))
    if page.page + 1 < page.pages:
        buttons.append(("Далее ›", route(page.page + 1)))
    return buttons


async def edit_menu(message, text, markup):
    try:
        await message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


async def edit_saved(bot, data, text, markup):
    try:
        await bot.edit_message_text(chat_id=data["panel_chat_id"], message_id=data["panel_message_id"],
                                    text=text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


async def delete_private_input(message):
    try:
        await message.delete()
    except TelegramAPIError:
        pass
