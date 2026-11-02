"""Shared safety UX for text, photos, documents and access middleware."""
from AI.moderation import SAFETY_REFUSAL
from Handlers.ui import keyboard


def appeal_keyboard(sanction_id):
    if sanction_id is None:
        return None
    return keyboard([("📨 Обжаловать блокировку", f"appeal:open:{sanction_id}")])


async def send_moderation_refusal(message, error):
    # Keep the safety refusal itself exact and separate from warning notices.
    # A third warning invalidates RAM generations; it must not suppress this UX.
    await message.answer(SAFETY_REFUSAL)
    outcome = error.outcome
    if outcome is not None and outcome.created:
        text = ("⚠️ Вы получили предупреждение за нарушение правил AiS."
                f"\nПредупреждений: {outcome.active_warning_count}/3.")
        if outcome.resulting_user_state == "blocked":
            text += "\nВаш доступ заблокирован."
        await message.answer(text, reply_markup=appeal_keyboard(outcome.sanction_id))
