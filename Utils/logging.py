from __future__ import annotations

import hashlib
import logging


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def safe_conversation_id(chat_id: int, user_id: int) -> str:
    raw = f"{chat_id}:{user_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]
