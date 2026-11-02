from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from AI.providers import model_for_backend
from Handlers.flows import ActivationFlow
from Handlers.menus import main_menu
from Handlers.ui import date_label, delete_private_input, edit_menu, edit_saved, keyboard, limit_label, safe
from app_context import AppContext
from Storage.models import AccessDeniedError
from Services.users import access_message


router = Router(name="accounts")


async def _private(callback):
    if callback.message is None or callback.message.chat.type != "private":
        await callback.answer("Управление доступом доступно в личном чате с ботом.", show_alert=True)
        return False
    return True


def trusted_view(account):
    application = account.get("application")
    trusted = account["tier"] == "TRUSTED"
    text = ("<b>⭐ Trusted</b>\n\n" + ("✅ Trusted активен." if trusted else "Ваш тариф: Standard.")
            + f"\nДневной лимит: {limit_label(account['effective_daily_limit'])}"
            + "\nPremium Models подключаются отдельно. Блокировки и ограничения действуют на всех тарифах.")
    if application and not trusted:
        statuses = {"pending": "⏳ Заявка ожидает решения", "approved": "Заявка одобрена", "rejected": "Заявка отклонена"}
        text += f"\n\n{statuses[application['status']]}\n{date_label(application['created_at'])}"
    rows = []
    if not trusted:
        if not application or application["status"] != "pending":
            rows.append([("📨 Запросить Trusted", "acct:apply")])
        rows.append([("🔑 Ввести код активации", "acct:premium")])
    rows.extend([[("ℹ️ Статус", "acct:status")], [("⬅️ В меню", "main_menu")]])
    return text, keyboard(*rows)


def premium_models_view(account):
    if account["premium_models_allowed"]:
        return "<b>🤖 Premium Models</b>\n\n✅ Доступ активен и сохранён. Выберите Premium-модель в каталоге.", keyboard(
            [("🤖 Выбрать модель", "models")], [("⬅️ В меню", "main_menu")])
    return "<b>🔒 Premium Models</b>\n\nДля этих моделей нужен код доступа. Trusted не открывает Premium Models автоматически.", keyboard(
        [("🔑 Ввести код доступа", "acct:premium_models_code")], [("⬅️ В меню", "main_menu")])


async def begin_activation(callback, state, app_context, purpose, *, provider=None, model_key=None):
    if not await _private(callback):
        return False
    await state.set_state(ActivationFlow.code)
    await state.set_data({"purpose": purpose, "provider": provider, "model_key": model_key,
                          "panel_chat_id": callback.message.chat.id, "panel_message_id": callback.message.message_id,
                          "expires_at": time.time() + 900})
    title = "🔒 Premium Models" if purpose in {"grok", "premium_models"} else "⭐ Активация Trusted"
    await edit_menu(callback.message, f"<b>{title}</b>\n\nВведите код отдельным сообщением. Мы попробуем удалить сообщение с кодом.",
                    keyboard([("Отмена", "acct:cancel")]))
    return True


@router.callback_query(F.data.startswith("acct:"))
async def account_callback(callback: CallbackQuery, app_context: AppContext, state: FSMContext):
    if not await _private(callback):
        return
    command = callback.data.split(":", 1)[1]
    if command in {"premium", "grok_code", "premium_models_code"}:
        await begin_activation(callback, state, app_context, "premium" if command == "premium" else "premium_models")
    else:
        await state.clear()
        if command == "apply":
            await app_context.accounts.apply_trusted(callback.from_user.id)
            app_context.notifications.wake()
        account = await app_context.accounts.status(callback.from_user.id)
        if command in {"trusted", "apply"}:
            text, markup = trusted_view(account)
        elif command in {"grok", "premium_models"}:
            text, markup = premium_models_view(account)
        elif command == "status":
            tier = "⭐ Trusted" if account["tier"] == "TRUSTED" else "Standard"
            text = (f"<b>👤 Мой доступ</b>\n\nТариф: {tier}\nЛимит: {limit_label(account['effective_daily_limit'])}"
                    f"\nAI-вызовы сегодня: {account['requests_today']}"
                    f"\nHigher-cost: {account['higher_cost_requests_today']}/{account['higher_cost_daily_limit']}"
                    f"\nPremium Models: {'✅ открыты' if account['premium_models_allowed'] else '🔒 закрыты'}"
                    "\n\nЛимит обновляется в 00:00 UTC.")
            markup = keyboard([("⭐ Trusted", "acct:trusted"), ("🤖 Premium Models", "acct:premium_models")], [("⬅️ В меню", "main_menu")])
        elif command == "cancel":
            text, markup = "<b>AiS</b>\n\nВыберите действие:", main_menu()
        else:
            await callback.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
            return
        await edit_menu(callback.message, text, markup)
    await callback.answer()


@router.message(ActivationFlow.code)
async def activation_message(message: Message, app_context: AppContext, state: FSMContext):
    data = await state.get_data()
    if message.chat.type != "private":
        await state.clear()
        return
    if not message.text:
        await message.answer("Введите код текстом или нажмите «Отмена».")
        return
    # No raw code is put in FSM, history, repositories, or user-facing errors.
    await delete_private_input(message)
    if data.get("expires_at", 0) < time.time() or data.get("purpose") not in {"grok", "premium_models", "premium"}:
        await state.clear()
        await message.answer("Диалог активации истёк. Откройте нужный раздел снова.", reply_markup=main_menu())
        return
    result = await app_context.accounts.activate(message.from_user.id, data["purpose"], message.text)
    if result.status in {"activated", "already"}:
        await state.clear()
        app_context.notifications.wake()
        if data["purpose"] in {"grok", "premium_models"} and data.get("model_key"):
            try:
                await app_context.users.select_model((message.chat.id, message.from_user.id), data["provider"], data["model_key"])
            except AccessDeniedError as error:
                # Activation grants a permission, not quota or a ban exemption.
                text = "✅ Доступ к Premium Models сохранён.\n\n" + access_message(
                    error.decision, app_context.settings.support_contact
                )
            else:
                model = model_for_backend(data["provider"], data["model_key"])
                text = f"✅ Premium Models открыты. Выбрана модель <b>{safe(model.display_name)}</b>.\n\nКонтекст очищен. Напишите сообщение."
        else:
            text = "✅ Доступ к Premium Models сохранён." if data["purpose"] in {"grok", "premium_models"} else "⭐ Trusted активирован."
        await edit_saved(message.bot, data, text, main_menu())
    elif result.status == "disabled":
        await state.clear()
        await edit_saved(message.bot, data, "Активация кодом сейчас недоступна. Обратитесь к администратору.", main_menu())
    else:
        text = (f"⏳ Слишком много попыток. Повторите после {date_label(result.locked_until)}." if result.status == "cooldown"
                else f"Код не подошёл. Осталось попыток: {result.remaining_attempts}.\n\nПопробуйте ещё раз или нажмите «Отмена».")
        await edit_saved(message.bot, data, text, keyboard([("Отмена", "acct:cancel")]))
