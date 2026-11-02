import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from openai import APIConnectionError
from AI.client import AISAPIError

from Storage.models import AccessDeniedError, utc_timestamp
from Tests.storage_support import StorageTestCase
from Tests import test_usage_persistence as usage_fixtures
from Tests.test_usage_persistence import UpstreamError, completion


class InferenceLimitTests(StorageTestCase):
    service = usage_fixtures.UsagePersistenceTests.service
    request = usage_fixtures.UsagePersistenceTests.request

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.add_user()

    async def test_standard_allows_50_real_inferences_then_denies_51st(self):
        service, create = self.service([completion()] * 51)
        for _ in range(50):
            await service.ask_text(**self.request())
        with self.assertRaises(AccessDeniedError) as error:
            await service.ask_text(**self.request())
        self.assertEqual(error.exception.decision.code, "limit_exceeded")
        self.assertEqual(create.await_count, 50)
        self.assertEqual((await self.accounts.status(2))["requests_today"], 50)
        await self.reopen()
        self.assertEqual((await self.accounts.status(2))["requests_today"], 50)

    async def test_trusted_500_boundary_uses_inference_history(self):
        await self.admin.act(999, "grant_trusted", user_id=2, reason="Trusted quota")
        timestamp = utc_timestamp()

        def seed(c):
            c.executemany("INSERT INTO usage_requests (request_id, telegram_user_id, timestamp, model, provider, status, counts_toward_limit) "
                          "VALUES (?, 2, ?, 'gpt_oss_120b', 'amazon_bedrock', 'succeeded', 0)", [(f"r{n}", timestamp) for n in range(499)])
            c.executemany("INSERT INTO usage_attempts (attempt_id, request_id, timestamp, model, provider, status, telegram_user_id) "
                          "VALUES (?, ?, ?, 'gpt_oss_120b', 'amazon_bedrock', 'succeeded', 2)", [(f"a{n}", f"r{n}", timestamp) for n in range(499)])
        await self.storage.database.run(seed, write=True)
        service, create = self.service([completion(), completion()])
        await service.ask_text(**self.request())
        with self.assertRaises(AccessDeniedError):
            await service.ask_text(**self.request())
        create.assert_awaited_once()
        self.assertEqual((await self.accounts.status(2))["requests_today"], 500)

    async def test_pre_upstream_errors_and_access_denials_do_not_consume_quota(self):
        service, create = self.service([completion()])
        with self.assertRaises(ValueError):
            await service.ask_image(**self.request(), image_bytes=None, mime_type="image/jpeg")
        with self.assertRaises(AccessDeniedError):
            await service.ask_text(**self.request(model="grok_4_6"))
        await self.admin.act(999, "block", user_id=2, reason="Ban")
        with self.assertRaises(AccessDeniedError):
            await service.ask_text(**self.request())
        create.assert_not_awaited()
        self.assertEqual((await self.accounts.status(2))["requests_today"], 0)

    async def test_local_sdk_and_connection_failures_refund_reservation(self):
        connect = APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
        connect.__cause__ = httpx.ConnectError("not connected")
        for failure in (ValueError("local validation"), connect, UpstreamError(429)):
            service, _ = self.service([failure])
            with self.assertRaises(ValueError if isinstance(failure, ValueError) else AISAPIError):
                await service.ask_text(**self.request())
        self.assertEqual((await self.accounts.status(2))["requests_today"], 0)
        attempts = await self.rows("SELECT counts_toward_limit FROM usage_attempts")
        self.assertTrue(all(row["counts_toward_limit"] == 0 for row in attempts))

    async def test_gateway_image_consumes_one_main_inference_quota(self):
        await self.admin.act(999, "set_limit", user_id=2, daily_request_limit=1, reason="Single inference")
        service, create = self.service([completion()])
        await service.ask_image(**self.request(), image_bytes=b"image", mime_type="image/jpeg")
        self.assertEqual(create.await_count, 1)
        attempts = await self.rows("SELECT model, counts_toward_limit FROM usage_attempts")
        self.assertEqual(attempts[0]["model"], "gpt_oss_120b")
        self.assertEqual([row["counts_toward_limit"] for row in attempts], [1])
        self.assertEqual((await self.accounts.status(2))["requests_today"], 1)

    async def test_parallel_chats_share_atomic_user_quota(self):
        await self.admin.act(999, "set_limit", user_id=2, daily_request_limit=3, reason="Concurrent test")
        service, create = self.service([completion()] * 8)
        requests = []
        for n in range(8):
            request = self.request()
            request["conversation_key"] = (100 + n, 2)
            request["conversation_id"] = self.state.current_conversation_id(request["conversation_key"])
            requests.append(service.ask_text(**request))
        results = await asyncio.gather(*requests, return_exceptions=True)
        self.assertEqual(sum(isinstance(result, AccessDeniedError) for result in results), 5)
        self.assertEqual(create.await_count, 3)
        self.assertEqual((await self.accounts.status(2))["requests_today"], 3)

    async def test_daily_quota_uses_utc_attempt_day(self):
        await self.admin.act(999, "set_limit", user_id=2, daily_request_limit=1, reason="Daily reset")
        service, create = self.service([completion(), completion()])
        yesterday = utc_timestamp(datetime.now(timezone.utc) - timedelta(days=1))
        with patch("Services.requests.utc_timestamp", return_value=yesterday):
            await service.ask_text(**self.request())
        await service.ask_text(**self.request())
        with self.assertRaises(AccessDeniedError):
            await service.ask_text(**self.request())
        self.assertEqual(create.await_count, 2)
        self.assertEqual((await self.accounts.status(2))["requests_today"], 1)

    async def test_exhausted_quota_keeps_existing_conversation_context(self):
        await self.admin.act(999, "set_limit", user_id=2, daily_request_limit=1, reason="One call")
        service, _ = self.service([completion()])
        await service.ask_text(**self.request())
        before = list(self.state.histories[(10, 2)])
        with self.assertRaises(AccessDeniedError):
            await service.ask_text(**self.request())
        self.assertEqual(self.state.histories[(10, 2)], before)
