from __future__ import annotations

import json

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app_context import AppContext


router = Router(name="admin")

HELP = (
    "Admin commands (private chat; numeric Telegram user IDs):\n"
    "/admin user <id>\n/admin events <id>\n/admin audit <id>\n/admin stats\n"
    "/admin block <id> <reason>\n/admin unblock <id> <reason>\n"
    "/admin restrict <id> <minutes> <reason>\n/admin unrestrict <id> <reason>\n"
    "/admin warn <id> <reason>\n/admin clear_warnings <id> <reason>\n"
    "/admin grant_premium <id> <reason>\n/admin revoke_premium <id> <reason>\n"
    "/admin limit <id> <requests_per_day|off> <reason>\n"
    "/admin review <event_id> <open|resolved|dismissed> <reason>\n"
    "To link a user action to an event, prefix its reason with event:<event_id>.\n"
    "Reasons must be brief; do not paste conversations or credentials."
)


@router.message(Command("admin_legacy"))
async def admin_handler(message: Message, app_context: AppContext) -> None:
    actor = message.from_user.id
    if not app_context.admin.is_admin(actor):
        await message.answer("⛔ Administrator access required.")
        return
    if message.chat.type != "private":
        await message.answer("Use admin commands in a private chat with the bot.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(HELP.replace("/admin ", "/admin_legacy "))
        return
    action = parts[1].lower()
    args = parts[2:]
    try:
        if action == "stats":
            value = await app_context.admin.runtime_counts(actor)
        elif action in {"user", "events", "audit"}:
            if len(args) != 1:
                raise ValueError("Provide one Telegram user ID")
            method = getattr(app_context.admin, action)
            value = await method(actor, int(args[0]))
        else:
            if len(args) < 2:
                raise ValueError("Provide an ID and a reason")
            target_id = int(args.pop(0))
            kwargs = {"user_id": target_id}
            if action == "restrict":
                kwargs["minutes"] = int(args.pop(0))
            elif action == "limit":
                limit = args.pop(0)
                kwargs["daily_request_limit"] = None if limit == "off" else int(limit)
                action = "set_limit"
            elif action == "review":
                kwargs = {"event_id": target_id, "review_status": args.pop(0)}
                action = "review_moderation"
            if args and args[0].startswith("event:") and action != "review_moderation":
                kwargs["event_id"] = int(args.pop(0).split(":", 1)[1])
            result = await app_context.admin.act(actor, action, reason=" ".join(args), **kwargs)
            await message.answer(f"✅ Applied. Audit ID: {result.audit_id}")
            return
        # Bounded metadata only, not a transcript export. No parse mode is used.
        await message.answer(json.dumps(value, ensure_ascii=False, indent=2)[:3800])
    except (ValueError, IndexError):
        await message.answer("Invalid command, target, or reason.\n\n" + HELP.replace("/admin ", "/admin_legacy "))
