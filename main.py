import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from Admins.service import AdminService
from AI.adaptive_safety import AdaptiveModerator
from AI.client import AISClient
from AI.input_moderation import InputModerator
from AI.models import MODEL_REGISTRY
from AI.router import AIRouter
from AI.safety_client import RemoteSafetyClient
from app_context import AppContext
from config import Settings
from Handlers import root_router
from Handlers.auth import AuthMiddleware
from Handlers.control import ControlFlowMiddleware
from Search.web_search import WebSearchService
from Services.access import AccountService
from Services.moderation import ModerationService
from Services.notifications import NotificationService
from Services.requests import AIRequestService
from Services.safety_shadow import SafetyReviewCache, SafetyShadow
from Services.users import UserService
from state import BotState
from Storage.sqlite import SQLiteStorage
from Utils.logging import configure_logging
from Utils.privacy import known_secrets

logger = logging.getLogger(__name__)


def build_application(settings: Settings) -> tuple[Bot, Dispatcher, AppContext]:
    state = BotState()
    global_semaphore = asyncio.Semaphore(settings.ai_max_concurrent_requests)
    client = AISClient(
        api_key=settings.ais_api_key,
        base_url=settings.ais_api_base_url,
        timeout=settings.ai_request_timeout_seconds,
        retry_count=settings.ai_retry_count,
    )
    ai_router = AIRouter(
        client=client,
        search=WebSearchService(settings.tavily_api_key),
        state=state,
        max_concurrent_requests=settings.ai_max_concurrent_requests,
        default_max_output_tokens=settings.ai_default_max_output_tokens,
        semaphore=global_semaphore,
    )
    storage = SQLiteStorage(settings.sqlite_path, settings.sqlite_busy_timeout_ms,
                            standard_limit=settings.standard_daily_limit, trusted_limit=settings.trusted_daily_limit or None,
                            higher_cost_limit=settings.higher_cost_daily_limit)
    users = UserService(storage.users, state)
    accounts = AccountService(storage.accounts, settings)
    redactions = known_secrets(settings.bot_token, settings.grok_access_code, settings.premium_activation_code,
                              settings.ais_api_key, settings.tavily_api_key)
    review_cache = SafetyReviewCache(redactions)
    safety_client = RemoteSafetyClient(gateway=client)
    adaptive = AdaptiveModerator(safety_client, global_semaphore)
    shadow = None
    if settings.safety_shadow_enabled:
        shadow_client = RemoteSafetyClient(gateway=client)
        shadow = SafetyShadow(storage.moderation,
            AdaptiveModerator(shadow_client, asyncio.Semaphore(1)),
            review_cache,
            queue_size=settings.safety_shadow_queue_size,
            timeout_seconds=settings.safety_shadow_timeout_seconds,
        )
    moderation = ModerationService(storage.moderation, settings.admin_user_ids, secrets=redactions,
        input_moderator=InputModerator(safety_client, global_semaphore),
        adaptive_moderator=adaptive, shadow=shadow, review_cache=review_cache)
    requests = AIRequestService(ai_router, users, storage.usage, moderation, state)
    admin = AdminService(
        storage.admins, storage.moderation, state, settings.admin_user_ids,
        secrets=redactions, safety_fingerprint=adaptive.fingerprint,
    )
    bot = Bot(token=settings.bot_token)
    notifications = NotificationService(storage.notifications, bot)
    app_context = AppContext(
        settings=settings,
        state=state,
        ai_router=ai_router,
        storage=storage, users=users, moderation=moderation, requests=requests,
        admin=admin, client=client, safety_client=safety_client, accounts=accounts, notifications=notifications,
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["app_context"] = app_context
    auth = AuthMiddleware(
        state=state,
        users=users,
        accounts=accounts,
        support_contact=settings.support_contact,
    )
    control = ControlFlowMiddleware()
    dispatcher.message.outer_middleware(control)
    dispatcher.callback_query.outer_middleware(control)
    dispatcher.message.outer_middleware(auth)
    dispatcher.callback_query.outer_middleware(auth)
    dispatcher.include_router(root_router())
    return bot, dispatcher, app_context


async def main() -> None:
    configure_logging()
    settings = Settings.load()
    bot, dispatcher, app_context = build_application(settings)
    try:
        await app_context.start()
        logger.info(
            "AiSCloudBot started models=%s user_visible=%s max_concurrency=%s database=%s",
            len(MODEL_REGISTRY),
            sum(model.user_visible for model in MODEL_REGISTRY.values()),
            settings.ai_max_concurrent_requests,
            settings.sqlite_path,
        )
        await dispatcher.start_polling(bot)
    finally:
        try:
            await app_context.close()
        finally:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
