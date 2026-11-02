from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from AI.providers import (
    available_providers,
    model_for_backend,
)
from Handlers.menus import (
    creator_models_menu,
    main_menu,
    provider_creators_menu,
    providers_menu,
)
from app_context import AppContext
from Handlers.accounts import begin_activation
from Handlers.ui import edit_menu
from Storage.models import AccessDeniedError
from Services.users import access_message


router = Router(name="models")


def _key(callback: CallbackQuery) -> tuple[int, int]:
    return callback.message.chat.id, callback.from_user.id


@router.callback_query(F.data == "models")
async def providers_handler(callback: CallbackQuery, app_context: AppContext) -> None:
    await callback.message.edit_text(
        "🤖 Выберите провайдера:",
        reply_markup=providers_menu(app_context.settings),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("provider:"))
async def provider_handler(callback: CallbackQuery, app_context: AppContext) -> None:
    key = callback.data.split(":", 1)[1]
    available = dict(available_providers())
    if key not in available:
        await callback.answer("Провайдер недоступен", show_alert=True)
        return
    account = await app_context.accounts.status(callback.from_user.id)
    await callback.message.edit_text(
        f"{available[key]}\nВыберите семейство моделей:",
        reply_markup=provider_creators_menu(key, app_context.settings, premium_models_allowed=bool(account["premium_models_allowed"])),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("creator:"))
async def creator_handler(callback: CallbackQuery, app_context: AppContext) -> None:
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Список моделей недоступен", show_alert=True)
        return
    provider, creator = parts[1:]
    account = await app_context.accounts.status(callback.from_user.id)
    await callback.message.edit_text(
        f"{'Grok' if creator.casefold() == 'xai' else creator}\nВыберите модель:",
        reply_markup=creator_models_menu(provider, creator, app_context.settings, premium_models_allowed=bool(account["premium_models_allowed"])),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("model:"))
async def select_model(callback: CallbackQuery, app_context: AppContext, state: FSMContext | None = None) -> None:
    parts = callback.data.split(":", 3)
    if len(parts) != 4:
        await callback.answer("Модель недоступна", show_alert=True)
        return
    provider, creator, model_key = parts[1:]
    try:
        model = model_for_backend(provider, model_key)
    except KeyError:
        await callback.answer("Модель недоступна", show_alert=True)
        return
    if not model.user_visible:
        await callback.answer("Модель недоступна", show_alert=True)
        return
    try:
        await app_context.users.select_model(_key(callback), provider, model_key)
    except AccessDeniedError as error:
        if error.decision.code in {"premium_locked", "grok_locked"} and state is not None:
            if await begin_activation(callback, state, app_context, "premium_models", provider=provider, model_key=model_key):
                await callback.answer("🔒 Нужен Premium Models access")
        else:
            await callback.answer(access_message(error.decision, app_context.settings.support_contact), show_alert=True)
        return
    if state is not None:
        await state.clear()
    await callback.message.edit_text(
        f"✅ Модель: {model.display_name}\n\nНапишите сообщение. При смене модели контекст начинается заново.",
        reply_markup=main_menu(),
    )
    await callback.answer("Модель выбрана")


@router.callback_query(F.data == "random_model")
async def random_model_handler(callback: CallbackQuery, app_context: AppContext, state: FSMContext | None = None) -> None:
    try:
        model = await app_context.users.random_model(_key(callback))
    except AccessDeniedError as error:
        await callback.answer(access_message(error.decision, app_context.settings.support_contact), show_alert=True)
        return
    except ValueError:
        await callback.answer("Сейчас нет доступных моделей. Проверьте доступ и дневную квоту.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    await edit_menu(callback.message, f"🎲 Выпала модель: {model.display_name}\n\nНапишите сообщение.", main_menu())
    await callback.answer("Модель выбрана")


@router.callback_query(F.data == "main_menu")
async def main_menu_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_menu(callback.message, "<b>AiS · Главное меню</b>\n\nВыберите действие:", main_menu())
    await callback.answer()


@router.callback_query(F.data == "new_chat")
async def new_chat_handler(callback: CallbackQuery, app_context: AppContext, state: FSMContext | None = None) -> None:
    if state is not None:
        await state.clear()
    chat_id, user_id = _key(callback)
    app_context.state.reset_conversation(chat_id, user_id)
    await callback.answer("Контекст очищен")
    await edit_menu(callback.message, "<b>💬 Новый чат</b>\n\nКонтекст очищен. Модель и настройки сохранены.", main_menu())


@router.callback_query(F.data == "support")
async def support_handler(callback: CallbackQuery, app_context: AppContext) -> None:
    contact = escape(app_context.settings.support_contact)
    contact_text = (
        f"Напишите нам: {contact}."
        if contact
        else "Свяжитесь с владельцем бота для настройки поддержки."
    )
    try:
        await callback.message.edit_text(
            f"✉️ Поддержка\n\n{contact_text}",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("page:"))
async def page_handler(callback: CallbackQuery, app_context: AppContext) -> None:
    try:
        page = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    key = (callback.message.chat.id, callback.message.message_id)
    entry = app_context.state.response_pages.get(key)
    if entry and entry.conversation.key[1] != callback.from_user.id:
        await callback.answer("This response belongs to another user.", show_alert=True)
        return
    if not entry or not app_context.state.is_current(entry.conversation):
        app_context.state.response_pages.pop(key, None)
        await callback.answer("This response is no longer available.", show_alert=True)
        return
    pages = entry.pages
    if not 0 <= page < len(pages):
        await callback.answer()
        return
    from Handlers.menus import pages_keyboard

    await callback.message.edit_text(
        pages[page],
        parse_mode="HTML",
        reply_markup=pages_keyboard(page, len(pages)),
    )
    await callback.answer()


@router.callback_query(F.data == "page_info")
async def page_info_handler(callback: CallbackQuery) -> None:
    await callback.answer()
