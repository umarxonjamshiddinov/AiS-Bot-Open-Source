import asyncio

from aiogram import BaseMiddleware


class ControlFlowMiddleware(BaseMiddleware):
    """Serialize short control dialogs, never hold their lock during AI chat.

    FSM's default global event isolation would also block New Chat while the
    previous inference is running. Refresh raw_state after acquiring this lock
    so queued confirmations/code messages cannot reuse stale FSM state.
    """

    def __init__(self):
        self._locks = {}

    async def __call__(self, handler, event, data):
        fsm = data.get("state")
        user = getattr(event, "from_user", None)
        message = getattr(event, "message", None) or event
        chat = getattr(message, "chat", None)
        if fsm is None or user is None or chat is None:
            return await handler(event, data)
        before = await fsm.get_state()
        callback_data = getattr(event, "data", None) or ""
        text = getattr(event, "text", None) or ""
        control = callback_data.startswith(("adm:", "acct:", "appeal:")) or text.split(" ", 1)[0].split("@", 1)[0] in {"/admin", "/cancel", "/menu", "/start"}
        if before is None and not control:
            return await handler(event, data)
        key = (chat.id, user.id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            current = await fsm.get_state()
            data["raw_state"] = current
            data["control_flow_expired"] = bool(before and current is None and not callback_data and not text.startswith("/"))
            return await handler(event, data)
