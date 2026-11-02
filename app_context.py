import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from Admins.service import AdminService
from AI.client import AISClient
from AI.router import AIRouter
from AI.safety_client import RemoteSafetyClient
from config import Settings
from Services.access import AccountService
from Services.moderation import ModerationService
from Services.notifications import NotificationService
from Services.requests import AIRequestService
from Services.users import UserService
from state import BotState
from Storage.interfaces import StorageBackend
from Storage.models import utc_timestamp


@dataclass(slots=True)
class AppContext:
    settings: Settings
    state: BotState
    ai_router: AIRouter
    storage: StorageBackend
    users: UserService
    moderation: ModerationService
    requests: AIRequestService
    admin: AdminService
    client: AISClient
    safety_client: RemoteSafetyClient
    accounts: AccountService
    notifications: NotificationService
    _maintenance_task: asyncio.Task | None = field(default=None, init=False)

    async def start(self) -> None:
        await self.client.validate_catalog()
        await self.storage.acquire_runtime()
        await self.storage.open()
        await self.storage.usage.recover_interrupted(utc_timestamp())
        await self._purge()
        self._maintenance_task = asyncio.create_task(self._maintenance(), name="storage-retention")
        self.notifications.start()
        self.moderation.start_shadow()

    async def _purge(self) -> None:
        now = datetime.now(timezone.utc)
        await self.storage.purge(
            usage_before=utc_timestamp(now - timedelta(days=self.settings.usage_retention_days)),
            moderation_before=utc_timestamp(now - timedelta(days=self.settings.moderation_retention_days)),
            audit_before=utc_timestamp(now - timedelta(days=self.settings.audit_retention_days)),
        )

    async def _maintenance(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                await self._purge()
            except Exception as error:
                logging.getLogger(__name__).error("Retention failed type=%s", type(error).__name__)

    async def close(self) -> None:
        await self.notifications.close()
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
            self._maintenance_task = None
        try:
            await self.requests.close()
            await self.moderation.close_shadow()
            await self.client.close()
            await self.safety_client.close()
        finally:
            await self.storage.close()
