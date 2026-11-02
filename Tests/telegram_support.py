from datetime import datetime, timezone

from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message


class LocalTelegramSession(BaseSession):
    """No network: exercise aiogram dispatch, FSM filters and actual Bot methods."""

    def __init__(self):
        super().__init__()
        self.methods = []
        self.sent = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.methods.append(method)
        if isinstance(method, SendMessage):
            message = Message(message_id=len(self.sent) + 1, date=datetime.now(timezone.utc),
                              chat=Chat(id=int(method.chat_id), type="private"), text=method.text,
                              reply_markup=method.reply_markup)
            self.sent.append(message)
            return message
        return True

    async def stream_content(self, *args, **kwargs):
        yield b""
