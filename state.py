from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import uuid4


ConversationKey = tuple[int, int]
ConversationId = str


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    key: ConversationKey
    conversation_id: ConversationId


@dataclass(slots=True)
class PaginationEntry:
    pages: list[str]
    conversation: ConversationSnapshot


@dataclass(slots=True)
class BotState:
    """Ephemeral conversation/runtime objects only; identity and access live in storage."""
    histories: dict[ConversationKey, list[dict[str, str]]] = field(
        default_factory=dict
    )
    conversation_ids: dict[ConversationKey, ConversationId] = field(
        default_factory=dict
    )
    document_contexts: dict[ConversationKey, object] = field(default_factory=dict)
    media_contexts: dict[ConversationKey, object] = field(default_factory=dict)
    temporary_contexts: dict[ConversationKey, object] = field(default_factory=dict)
    response_pages: dict[tuple[int, int], PaginationEntry] = field(
        default_factory=dict
    )
    user_locks: dict[ConversationKey, asyncio.Lock] = field(default_factory=dict)
    safety_refusals: dict[ConversationKey, list[str]] = field(default_factory=dict)

    def current_conversation_id(self, key: ConversationKey) -> ConversationId:
        conversation_id = self.conversation_ids.get(key)
        if conversation_id is None:
            conversation_id = uuid4().hex
            self.conversation_ids[key] = conversation_id
        return conversation_id

    def snapshot(self, key: ConversationKey) -> ConversationSnapshot:
        return ConversationSnapshot(key, self.current_conversation_id(key))

    def is_current(self, snapshot: ConversationSnapshot) -> bool:
        return self.current_conversation_id(snapshot.key) == snapshot.conversation_id

    def lock_for(self, key: ConversationKey) -> asyncio.Lock:
        lock = self.user_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.user_locks[key] = lock
        return lock

    def history_for(self, key: ConversationKey) -> list[dict[str, str]]:
        return self.histories.setdefault(key, [])

    def remember_safety_refusal(self, snapshot: ConversationSnapshot, text: str) -> None:
        if self.is_current(snapshot):
            # Historical moderator context only; never supplied as chat history.
            excerpt = text.encode("utf-8")[:1800].decode("utf-8", errors="ignore")
            if excerpt != text:
                excerpt += " [historical request truncated]"
            entries = self.safety_refusals.setdefault(snapshot.key, [])
            entries.append(excerpt)
            entries[:] = entries[-3:]

    def reset_conversation(self, chat_id: int, user_id: int) -> ConversationId:
        """Atomically freeze the old generation and clear conversation-scoped state."""
        key = (chat_id, user_id)
        new_conversation_id = uuid4().hex
        self.conversation_ids[key] = new_conversation_id
        self.histories.pop(key, None)
        self.document_contexts.pop(key, None)
        self.media_contexts.pop(key, None)
        self.temporary_contexts.pop(key, None)
        self.safety_refusals.pop(key, None)
        self.response_pages = {
            message_key: entry
            for message_key, entry in self.response_pages.items()
            if entry.conversation.key != key
        }
        # New-generation requests must not wait for a running old-generation request.
        # Already queued old requests keep their reference to the previous lock and
        # will be rejected by their generation check after acquiring it.
        self.user_locks[key] = asyncio.Lock()
        return new_conversation_id

    def reset_user_conversations(self, user_id: int) -> None:
        keys = set(self.conversation_ids) | set(self.histories) | set(self.user_locks)
        for key in keys:
            if key[1] == user_id:
                self.reset_conversation(*key)
