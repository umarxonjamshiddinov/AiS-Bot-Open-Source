import logging

from aiogram import Bot, F, Router
from aiogram.types import Message

from AI.errors import inference_error_message
from AI.input_moderation import ModerationUnavailableError
from AI.prompts import AIS_IDENTITY, identity_response, is_identity_question
from AI.providers import backend_display_name, model_for_backend
from AI.router import StaleConversationError
from AI.telemetry import ProviderModerationError
from app_context import AppContext
from Handlers.common import conversation_key, send_paginated
from Handlers.menus import providers_menu
from Handlers.moderation import appeal_keyboard, send_moderation_refusal
from Services.users import access_message
from Storage.models import AccessDeniedError
from Utils.logging import safe_conversation_id

router = Router(name="chat")
logger = logging.getLogger(__name__)


@router.message(F.text)
async def text_handler(message: Message, bot: Bot, app_context: AppContext) -> None:
    key = conversation_key(message)
    selection = await app_context.users.selection(key)
    if is_identity_question(message.text):
        model_key = selection.model_key
        if model_key is None:
            await message.answer(AIS_IDENTITY)
            return
        provider = selection.provider
        try:
            model = model_for_backend(provider, model_key)
        except KeyError:
            await message.answer(AIS_IDENTITY)
            return
        await message.answer(identity_response(model, backend_display_name(provider)))
        return
    conversation = app_context.state.snapshot(key)
    model_key = selection.model_key
    if model_key is None:
        await message.answer("🤖 Сначала выберите модель.", reply_markup=providers_menu(app_context.settings))
        return
    try:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        result = await app_context.requests.ask_text(
            conversation_key=key,
            conversation_id=conversation.conversation_id,
            model_key=model_key,
            provider=selection.provider,
            prompt=message.text,
            web_query=message.text,
        )
        await send_paginated(message, result.text, app_context.state, conversation, app_context.users)
    except StaleConversationError:
        return
    except AccessDeniedError as error:
        await message.answer(
            access_message(error.decision, app_context.settings.support_contact),
            reply_markup=appeal_keyboard(error.decision.sanction_id),
        )
    except ProviderModerationError as error:
        await send_moderation_refusal(message, error)
    except ModerationUnavailableError:
        if app_context.state.is_current(conversation):
            await message.answer("❌ Не удалось проверить запрос. Повторите позже или сократите сообщение. Предупреждение не начислено.")
    except Exception as error:
        logger.error(
            "Text request failed conversation=%s model=%s type=%s",
            safe_conversation_id(*key),
            model_key,
            type(error).__name__,
        )
        if app_context.state.is_current(conversation):
            await message.answer(inference_error_message(error))
