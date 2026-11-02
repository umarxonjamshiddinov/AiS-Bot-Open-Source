import unittest
from types import SimpleNamespace

from Handlers.common import send_paginated
from Handlers.models import new_chat_handler
from Handlers.start import start_handler
from state import BotState, PaginationEntry
from Tests.storage_support import StorageTestCase


class FakeSentMessage:
    def __init__(self, chat_id: int, message_id: int = 900) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.message_id = message_id
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


class FakeMessage:
    def __init__(self, chat_id: int, user_id: int, on_answer=None) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []
        self.on_answer = on_answer
        self.sent = FakeSentMessage(chat_id)

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        if self.on_answer is not None:
            self.on_answer()
        return self.sent

    async def edit_text(self, text, **kwargs):
        return await self.answer(text, **kwargs)


class FakeCallback:
    def __init__(self, chat_id: int, user_id: int) -> None:
        self.message = FakeMessage(chat_id, user_id)
        self.from_user = SimpleNamespace(id=user_id)
        self.callback_answers = []

    async def answer(self, text=None, **kwargs) -> None:
        self.callback_answers.append((text, kwargs))


def populate_conversation(state: BotState, key: tuple[int, int]) -> str:
    conversation = state.snapshot(key)
    state.histories[key] = [{"role": "user", "content": "old"}]
    state.document_contexts[key] = {"document": "old"}
    state.media_contexts[key] = {"image": "old"}
    state.temporary_contexts[key] = {"temporary": "old"}
    state.response_pages[(key[0], 77)] = PaginationEntry(
        pages=["old page"], conversation=conversation
    )
    state.lock_for(key)
    return conversation.conversation_id


class ResetTests(StorageTestCase):
    def assert_conversation_reset(
        self, state: BotState, key: tuple[int, int], old_id: str
    ) -> None:
        self.assertNotEqual(state.current_conversation_id(key), old_id)
        self.assertNotIn(key, state.histories)
        self.assertNotIn(key, state.document_contexts)
        self.assertNotIn(key, state.media_contexts)
        self.assertNotIn(key, state.temporary_contexts)
        self.assertFalse(
            any(entry.conversation.key == key for entry in state.response_pages.values())
        )

    async def test_start_performs_full_reset_and_preserves_model(self) -> None:
        state = BotState()
        key = (100, 10)
        other = (200, 20)
        old_id = populate_conversation(state, key)
        other_id = populate_conversation(state, other)
        other_history = list(state.histories[other])
        message = FakeMessage(*key)

        await self.add_user(key[1])
        await self.users.select_model(key, "amazon_bedrock", "deepseek_v3_1")

        await start_handler(message, SimpleNamespace(state=state))

        self.assert_conversation_reset(state, key, old_id)
        self.assertEqual((await self.users.selection(key)).model_key, "deepseek_v3_1")
        self.assertEqual(state.current_conversation_id(other), other_id)
        self.assertEqual(state.histories[other], other_history)
        self.assertIn(other, state.document_contexts)
        self.assertIn(other, state.media_contexts)
        self.assertIn(other, state.temporary_contexts)
        self.assertTrue(
            any(entry.conversation.key == other for entry in state.response_pages.values())
        )
        self.assertTrue(message.answers)

    async def test_new_chat_uses_the_same_full_reset(self) -> None:
        state = BotState()
        key = (300, 30)
        old_id = populate_conversation(state, key)
        callback = FakeCallback(*key)

        await self.add_user(key[1])
        await self.users.select_model(key, "amazon_bedrock", "deepseek_v3_1")

        await new_chat_handler(callback, SimpleNamespace(state=state))

        self.assert_conversation_reset(state, key, old_id)
        self.assertEqual((await self.users.selection(key)).model_key, "deepseek_v3_1")
        self.assertTrue(callback.callback_answers)
        self.assertTrue(callback.message.answers)

    async def test_stale_result_is_not_sent(self) -> None:
        state = BotState()
        key = (400, 40)
        old = state.snapshot(key)
        state.reset_conversation(*key)
        message = FakeMessage(*key)

        sent = await send_paginated(message, "old answer", state, old)

        self.assertFalse(sent)
        self.assertEqual(message.answers, [])
        self.assertEqual(state.response_pages, {})

    async def test_reset_during_telegram_send_deletes_stale_message(self) -> None:
        state = BotState()
        key = (500, 50)
        old = state.snapshot(key)
        message = FakeMessage(*key, on_answer=lambda: state.reset_conversation(*key))

        sent = await send_paginated(message, "old answer", state, old)

        self.assertFalse(sent)
        self.assertTrue(message.sent.deleted)
        self.assertEqual(state.response_pages, {})
