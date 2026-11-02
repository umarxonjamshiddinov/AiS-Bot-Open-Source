from aiogram import Router

from Handlers.admin import router as admin_router
from Handlers.admin_panel import router as admin_panel_router
from Handlers.accounts import router as accounts_router
from Handlers.appeals import router as appeals_router
from Handlers.chat import router as chat_router
from Handlers.media import router as media_router
from Handlers.models import router as models_router
from Handlers.start import router as start_router


def root_router() -> Router:
    router = Router(name="root")
    router.include_routers(
        start_router,
        admin_panel_router,
        admin_router,
        appeals_router,
        accounts_router,
        models_router,
        media_router,
        chat_router,
    )
    return router


__all__ = ["root_router"]
