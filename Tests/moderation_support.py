from uuid import uuid4

from AI.moderation import ModerationResult, ProviderModerationError
from Tests.storage_support import StorageTestCase
from Tests import test_usage_persistence as usage_fixtures
from Tests.test_usage_persistence import completion


class ModerationTestCase(StorageTestCase):
    service = usage_fixtures.UsagePersistenceTests.service
    request = usage_fixtures.UsagePersistenceTests.request

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.add_user(2)
        await self.add_user(999, username="owner")

    async def violation(self, user_id=2, response=None):
        service, create = self.service([response or completion(finish_reason="content_filter")])
        with self.assertRaises(ProviderModerationError) as raised:
            await service.ask_text(**self.request(user_id=user_id))
        create.assert_awaited_once()
        return raised.exception.outcome

    async def confirm(self, user_id=2, *, request_id=None, result=None):
        return await self.moderation.confirm(
            user_id=user_id, request_id=request_id or uuid4().hex, model="gpt_oss_120b", provider="amazon_bedrock",
            result=result or ModerationResult(True, "provider_finish_reason", "content_filter", "content_filter"))

    async def ban(self, user_id=2):
        for _ in range(3):
            result = await self.confirm(user_id)
        return result

    async def submit_appeal(self, user_id=2, explanation="This decision was mistaken."):
        block = await self.moderation.block_for_user(user_id)
        return await self.moderation.appeal(user_id, block["id"], explanation)
