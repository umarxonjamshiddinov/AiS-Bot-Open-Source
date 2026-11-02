import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from Admins.service import AdminService
from Services.moderation import ModerationService
from Services.users import UserService
from Services.access import AccountService
from Storage.models import UserProfile, utc_timestamp
from Storage.sqlite import SQLiteStorage
from state import BotState
from config import Settings
from AI.moderation import ModerationResult


class StorageTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        test_directory = Path(__file__).resolve().parents[1] / "data" / "test-runs"
        test_directory.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="ais-storage-test-", dir=test_directory)
        self.path = Path(self.temporary.name) / "application.sqlite3"
        await self.open_services()

    async def open_services(self):
        self.storage = SQLiteStorage(self.path)
        await self.storage.open()
        self.state = BotState()
        self.users = UserService(self.storage.users, self.state)
        self.admin = AdminService(self.storage.admins, self.storage.moderation,
                                  self.state, frozenset({999}), ("test-secret-value",))
        self.settings = Settings(bot_token="123456:fake-test-token", ais_api_key="fake",
                                 ais_api_base_url="https://ais.example/v1",
                                 tavily_api_key="fake", sqlite_path=self.path,
                                 admin_user_ids=frozenset({999}), grok_access_code="test-grok-code",
                                 premium_activation_code="test-premium-code")
        self.accounts = AccountService(self.storage.accounts, self.settings)
        self.moderation = ModerationService(self.storage.moderation, self.settings.admin_user_ids,
            (self.settings.bot_token, self.settings.grok_access_code, self.settings.premium_activation_code, "test-secret-value"))
        # Legacy repository/product tests explicitly isolate product telemetry.
        # InputModerator's real parser + technical calls are exercised separately.
        self.moderation.input_moderator = SimpleNamespace(check=AsyncMock(return_value=ModerationResult(
            False, "test_fixture", stage="input")))
        self.admin.secrets += (self.settings.bot_token, self.settings.grok_access_code, self.settings.premium_activation_code)

    async def reopen(self):
        await self.storage.close()
        await self.open_services()

    async def add_user(self, user_id=2, *, authorized=True, username="tester"):
        await self.storage.users.observe(UserProfile(user_id, username, "Tester"), utc_timestamp())
        if authorized:
            await self.users.authorize(user_id)

    async def rows(self, query, parameters=()):
        return await self.storage.database.run(
            lambda connection: [dict(row) for row in connection.execute(query, parameters)]
        )

    async def asyncTearDown(self):
        await self.storage.close()
        self.temporary.cleanup()
