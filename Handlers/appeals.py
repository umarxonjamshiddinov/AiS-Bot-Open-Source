from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from Handlers.flows import AppealFlow
from Handlers.moderation import appeal_keyboard
from Handlers.ui import delete_private_input, keyboard
from app_context import AppContext


router = Router(name="appeals")


def _private(message, user_id):
    return message is not None and message.chat.type == "private" and message.chat.id == user_id


def _status(status):
    return {"pending": "📨 Ваша апелляция ожидает решения администратора.",
            "approved": "✅ Ваша апелляция удовлетворена. Связанное предупреждение отменено.",
            "rejected": "❌ Ваша апелляция отклонена. Блокировка по этому решению остаётся."}[status]


@router.callback_query(F.data.startswith("appeal:"))
async def appeal_callback(callback: CallbackQuery, app_context: AppContext, state: FSMContext):
    uid = callback.from_user.id
    if not _private(callback.message, uid):
        await callback.answer("Откройте личный чат с ботом для подачи апелляции.", show_alert=True)
        return
    try:
        parts = callback.data.split(":")
        if parts == ["appeal", "cancel"]:
            await state.clear()
            await callback.answer("Подача апелляции отменена.")
            return
        if len(parts) != 3 or parts[1] not in {"open", "status"}:
            raise ValueError("Invalid appeal action")
        resource_id = int(parts[2])
        if resource_id <= 0:
            raise ValueError("Invalid reference")
        if parts[1] == "status":
            appeal = await app_context.moderation.appeal_for_user(uid, resource_id)
            if not appeal:
                raise ValueError("No owned appeal")
            await callback.answer(_status(appeal["status"]), show_alert=True)
            return
        block = await app_context.moderation.block_for_user(uid, resource_id)
        if not block:
            raise ValueError("No owned active automatic block")
        if block["appeal"]:
            await state.clear()
            await callback.answer(_status(block["appeal"]["status"]), show_alert=True)
            return
        await state.set_data({"appeal_sanction_id": block["id"]})
        await state.set_state(AppealFlow.explanation)
        await callback.message.answer(
            "📨 Кратко объясните, почему блокировка ошибочна (до 1000 символов). "
            "Не отправляйте пароли, коды доступа или другие секреты.",
            reply_markup=keyboard([("Отмена", "appeal:cancel")]))
        await callback.answer()
    except (ValueError, KeyError):
        await state.clear()
        await callback.answer("Апелляция недоступна или кнопка устарела.", show_alert=True)


@router.message(AppealFlow.explanation, F.text)
async def appeal_explanation(message: Message, app_context: AppContext, state: FSMContext):
    if not _private(message, message.from_user.id):
        await state.clear()
        return
    await delete_private_input(message)
    if not 1 <= len(message.text.strip()) <= 1000:
        await message.answer("Напишите объяснение длиной от 1 до 1000 символов.")
        return
    data = await state.get_data()
    try:
        result = await app_context.moderation.appeal(
            message.from_user.id, data.get("appeal_sanction_id"), message.text)
    except ValueError:
        await state.clear()
        await message.answer("Эта блокировка больше недоступна для обжалования.")
        return
    await state.clear()
    app_context.notifications.wake()
    await message.answer(_status(result["status"]), reply_markup=keyboard(
        [("📨 Статус апелляции", f"appeal:status:{result['id']}")]))


@router.message(AppealFlow.explanation)
async def appeal_fallback(message: Message):
    await message.answer("Отправьте краткое объяснение текстом или нажмите «Отмена».",
                         reply_markup=keyboard([("Отмена", "appeal:cancel")]))
