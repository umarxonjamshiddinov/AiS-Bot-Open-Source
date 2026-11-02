import asyncio
import unittest

from AI.router import AIRouter, StaleConversationError
from state import BotState


class FakeClient:
    def __init__(
        self,
        delay: float = 0.0,
        *,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.calls = []
        self.delay = delay
        self.started = started
        self.release = release
        self.active = 0
        self.max_active = 0

    async def complete(self, **kwargs) -> str:
        self.calls.append(kwargs)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.started is not None:
            self.started.set()
        try:
            if self.release is not None:
                await self.release.wait()
            elif self.delay:
                await asyncio.sleep(self.delay)
            return f"OK:{kwargs['model'].key}"
        finally:
            self.active -= 1


class FakeSearch:
    def __init__(self) -> None:
        self.queries = []

    async def search(self, query: str):
        self.queries.append(query)
        return [{"title": "Source", "url": "https://example.com", "content": "Fact"}]

    @staticmethod
    def context(results):
        return "URL: https://example.com\nContent: Fact"

    @staticmethod
    def append_real_sources(answer, results):
        return answer + "\nhttps://example.com"


def make_router(client=None, limit: int = 4):
    state = BotState()
    client = client or FakeClient()
    search = FakeSearch()
    return (
        AIRouter(
            client=client,
            search=search,
            state=state,
            max_concurrent_requests=limit,
            default_max_output_tokens=4096,

        ),
        state,
        client,
        search,
    )


def generation(state: BotState, key: tuple[int, int]) -> str:
    return state.current_conversation_id(key)


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def assert_native_vision(self, model_key: str) -> None:
        router, state, client, _ = make_router()
        key = (1, hash(model_key) % 10_000)
        result = await router.ask_image(
            conversation_key=key,
            conversation_id=generation(state, key),
            model_key=model_key,
            prompt="Что на фото?",
            image_bytes=b"image",
            mime_type="image/jpeg",
        )
        self.assertEqual(result.route, "ais_vision")
        self.assertEqual(result.inference_count, 1)
        self.assertFalse(result.helper_used)
        self.assertEqual([c["model"].key for c in client.calls], [model_key])

    async def assert_vision_fallback(self, model_key: str) -> None:
        # Text-only models receive the original image through the same gateway.
        await self.assert_native_vision(model_key)

    async def test_grok_uses_one_native_vision_inference(self) -> None:
        await self.assert_native_vision("grok_4_3")

    async def test_grok_4_6_uses_one_native_vision_inference(self) -> None:
        await self.assert_native_vision("grok_4_6")

    async def test_gemma_3_4b_uses_one_native_vision_inference(self) -> None:
        await self.assert_native_vision("gemma_3_4b_it")

    async def test_qwen_vl_selected_uses_one_native_inference(self) -> None:
        await self.assert_native_vision("qwen3_vl_235b_a22b")

    async def test_deepseek_v3_1_uses_qwen_fallback(self) -> None:
        await self.assert_vision_fallback("deepseek_v3_1")

    async def test_glm_5_uses_qwen_fallback(self) -> None:
        await self.assert_vision_fallback("glm_5")

    async def test_gpt_oss_120b_uses_qwen_fallback(self) -> None:
        await self.assert_vision_fallback("gpt_oss_120b")

    async def test_voxtral_mini_uses_qwen_fallback(self) -> None:
        await self.assert_vision_fallback("voxtral_mini_3b_2507")

    async def test_internal_safeguard_does_not_start_fallback(self) -> None:
        router, state, client, _ = make_router()
        key = (4, 4)
        self.assertEqual(router.visual_route("gpt_oss_safeguard_120b"), "internal_only")
        with self.assertRaisesRegex(ValueError, "Internal safeguard models"):
            await router.ask_image(
                conversation_key=key,
                conversation_id=generation(state, key),
                model_key="gpt_oss_safeguard_120b",
                prompt="Что на фото?",
                image_bytes=b"image",
                mime_type="image/jpeg",
            )
        self.assertEqual(client.calls, [])
        self.assertNotIn(key, state.histories)

    async def test_running_old_request_is_discarded_without_history(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        client = FakeClient(started=started, release=release)
        router, state, _, _ = make_router(client)
        key = (10, 1)
        old_id = generation(state, key)
        task = asyncio.create_task(
            router.ask_text(
                conversation_key=key,
                conversation_id=old_id,
                model_key="gpt_oss_120b",
                prompt="old",
            )
        )
        await started.wait()
        new_id = state.reset_conversation(*key)
        release.set()
        with self.assertRaises(StaleConversationError):
            await task
        self.assertNotEqual(old_id, new_id)
        self.assertNotIn(key, state.histories)

    async def test_queued_old_request_does_not_start_inference(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        client = FakeClient(started=started, release=release)
        router, state, _, _ = make_router(client)
        key = (11, 1)
        old_id = generation(state, key)
        first = asyncio.create_task(
            router.ask_text(
                conversation_key=key,
                conversation_id=old_id,
                model_key="gpt_oss_120b",
                prompt="running",
            )
        )
        await started.wait()
        queued = asyncio.create_task(
            router.ask_text(
                conversation_key=key,
                conversation_id=old_id,
                model_key="gpt_oss_120b",
                prompt="queued",
            )
        )
        await asyncio.sleep(0)
        state.reset_conversation(*key)
        release.set()
        results = await asyncio.gather(first, queued, return_exceptions=True)
        self.assertTrue(all(isinstance(item, StaleConversationError) for item in results))
        self.assertEqual(len(client.calls), 1)
        self.assertNotIn(key, state.histories)

    async def test_new_generation_works_while_old_result_is_frozen(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        old_client = FakeClient(started=started, release=release)
        router, state, _, _ = make_router(old_client)
        key = (12, 1)
        old_id = generation(state, key)
        old_task = asyncio.create_task(
            router.ask_text(
                conversation_key=key,
                conversation_id=old_id,
                model_key="gpt_oss_120b",
                prompt="old",
            )
        )
        await started.wait()
        new_id = state.reset_conversation(*key)
        router._client = FakeClient()
        result = await router.ask_text(
            conversation_key=key,
            conversation_id=new_id,
            model_key="gpt_oss_120b",
            prompt="new",
        )
        self.assertEqual(result.text, "OK:gpt_oss_120b")
        release.set()
        with self.assertRaises(StaleConversationError):
            await old_task
        self.assertEqual(state.histories[key][0]["content"], "new")

    async def test_same_model_different_users_run_concurrently(self) -> None:
        client = FakeClient(delay=0.05)
        router, state, _, _ = make_router(client)
        keys = ((20, 1), (30, 2))
        await asyncio.gather(
            *(
                router.ask_text(
                    conversation_key=key,
                    conversation_id=generation(state, key),
                    model_key="gpt_oss_120b",
                    prompt=str(key),
                )
                for key in keys
            )
        )
        self.assertEqual(client.max_active, 2)

    async def test_same_conversation_is_sequential(self) -> None:
        client = FakeClient(delay=0.03)
        router, state, _, _ = make_router(client)
        key = (40, 1)
        conversation_id = generation(state, key)
        await asyncio.gather(
            router.ask_text(
                conversation_key=key,
                conversation_id=conversation_id,
                model_key="gpt_oss_120b",
                prompt="one",
            ),
            router.ask_text(
                conversation_key=key,
                conversation_id=conversation_id,
                model_key="gpt_oss_120b",
                prompt="two",
            ),
        )
        self.assertEqual(client.max_active, 1)
        self.assertEqual(
            [m["role"] for m in state.histories[key]],
            ["user", "assistant", "user", "assistant"],
        )

    async def test_global_semaphore_caps_different_users(self) -> None:
        client = FakeClient(delay=0.04)
        router, state, _, _ = make_router(client, limit=2)
        keys = [(n, n) for n in range(3)]
        await asyncio.gather(
            *(
                router.ask_text(
                    conversation_key=key,
                    conversation_id=generation(state, key),
                    model_key="gpt_oss_120b",
                    prompt=str(key),
                )
                for key in keys
            )
        )
        self.assertEqual(client.max_active, 2)

    async def test_web_search_remains_model_agnostic(self) -> None:
        router, state, _, _ = make_router()
        key = (50, 50)
        result = await router.ask_text(
            conversation_key=key,
            conversation_id=generation(state, key),
            model_key="deepseek_v3_2",
            prompt="Найди последние новости",
        )
        self.assertTrue(result.web_search_used)
        self.assertIn("https://example.com", result.text)
