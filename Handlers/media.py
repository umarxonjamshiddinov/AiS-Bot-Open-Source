from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.types import Message

from AI.errors import inference_error_message
from AI.input_moderation import ModerationUnavailableError
from AI.models import Modality
from AI.providers import model_for_backend
from AI.router import StaleConversationError
from AI.safety_context import SafetyInput
from AI.telemetry import ProviderModerationError
from app_context import AppContext
from Handlers.common import conversation_key, send_paginated
from Handlers.menus import providers_menu
from Handlers.moderation import appeal_keyboard, send_moderation_refusal
from Reader.documents import ImageOnlyPdfError, extract_document, is_supported_document
from Services.users import access_message
from Storage.models import AccessDeniedError
from Utils.logging import safe_conversation_id

router = Router(name="media")
logger = logging.getLogger(__name__)


@router.message(F.photo)
async def photo_handler(message: Message, bot: Bot, app_context: AppContext) -> None:
    key = conversation_key(message)
    conversation = app_context.state.snapshot(key)
    selection = await app_context.users.selection(key)
    model_key = selection.model_key
    if model_key is None:
        await message.answer("🤖 Сначала выберите модель.", reply_markup=providers_menu(app_context.settings))
        return
    try:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        image_buffer = await bot.download_file(file.file_path)
        prompt = message.caption or (
            "Analyze this image and describe the most important details."
        )
        result = await app_context.requests.ask_image(
            conversation_key=key,
            conversation_id=conversation.conversation_id,
            model_key=model_key,
            provider=selection.provider,
            prompt=prompt,
            image_bytes=image_buffer.read(),
            mime_type="image/jpeg",
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
            "Image request failed conversation=%s model=%s type=%s",
            safe_conversation_id(*key),
            model_key,
            type(error).__name__,
        )
        if app_context.state.is_current(conversation):
            await message.answer(inference_error_message(error))


@router.message(F.document)
async def document_handler(message: Message, bot: Bot, app_context: AppContext) -> None:
    key = conversation_key(message)
    conversation = app_context.state.snapshot(key)
    document = message.document
    filename = document.file_name or "unknown"
    if not is_supported_document(filename):
        await message.answer(f"❌ Формат файла {filename} пока не поддерживается.")
        return
    selection = await app_context.users.selection(key)
    model_key = selection.model_key
    if model_key is None:
        await message.answer("🤖 Сначала выберите модель.", reply_markup=providers_menu(app_context.settings))
        return
    try:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        file = await bot.get_file(document.file_id)
        file_buffer = await bot.download_file(file.file_path)
        document_text = await asyncio.to_thread(
            extract_document, file_buffer.read(), filename
        )
        user_prompt = message.caption or (
            "Analyze this file, explain its contents, and highlight what matters."
        )
        content = (
            f"The user uploaded a file: {filename}\n\n"
            f"User request:\n{user_prompt}\n\n"
            f"--- FILE START ---\n{document_text}\n--- FILE END ---"
        )
        result = await app_context.requests.ask_text(
            conversation_key=key,
            conversation_id=conversation.conversation_id,
            model_key=model_key,
            provider=selection.provider,
            prompt=content,
            web_query=user_prompt,
            safety_input=SafetyInput(user_prompt, document_text, "document"),
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
    except ImageOnlyPdfError:
        if app_context.state.is_current(conversation):
            await message.answer("❌ В PDF нет доступного текста. Отправьте нужные страницы как фотографии.")
    except ValueError:
        if app_context.state.is_current(conversation):
            await message.answer("❌ Не удалось прочитать документ. Проверьте файл и его формат.")
    except Exception as error:
        logger.error(
            "Document request failed conversation=%s model=%s type=%s",
            safe_conversation_id(*key),
            model_key,
            type(error).__name__,
        )
        if app_context.state.is_current(conversation):
            await message.answer(inference_error_message(error))


@router.message(F.voice | F.audio)
async def audio_handler(message: Message, app_context: AppContext) -> None:
    selection = await app_context.users.selection(conversation_key(message))
    model_key = selection.model_key
    if model_key is None:
        await message.answer("🤖 Сначала выберите модель.", reply_markup=providers_menu(app_context.settings))
        return
    model = model_for_backend(selection.provider, model_key)
    if model.supports(Modality.SPEECH):
        await message.answer(
            "🎙 Обработка аудио пока недоступна. Отправьте текст или документ."
        )
    else:
        await message.answer(
            "🎙 Эта модель не обрабатывает аудио. Отправьте текст или документ."
        )


@router.message(F.video)
async def video_handler(message: Message, app_context: AppContext) -> None:
    selection = await app_context.users.selection(conversation_key(message))
    model_key = selection.model_key
    if model_key is None:
        await message.answer("🤖 Сначала выберите модель.", reply_markup=providers_menu(app_context.settings))
        return
    model = model_for_backend(selection.provider, model_key)
    if model.supports(Modality.VIDEO):
        await message.answer(
            "🎬 Обработка видео пока недоступна. Отправьте текст или фото."
        )
    else:
        await message.answer("🎬 Эта модель не обрабатывает видео. Отправьте текст или фото.")
