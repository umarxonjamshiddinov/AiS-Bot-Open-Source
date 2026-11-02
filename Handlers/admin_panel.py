from __future__ import annotations

import secrets
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from Admins import panels
from Admins import safety_panels
from Handlers.flows import AdminFlow
from Handlers.ui import delete_private_input, edit_menu, edit_saved, keyboard, safe, user_label
from app_context import AppContext


router = Router(name="admin_panel")


async def _guard(event, app_context):
    callback = isinstance(event, CallbackQuery) or hasattr(event, "data")
    message = event.message if callback else event
    if not app_context.admin.is_admin(event.from_user.id):
        text = "⛔ Доступ только для администратора."
    elif message is None or message.chat.type != "private":
        text = "Откройте админ-панель в личном чате с ботом."
    else:
        return True
    await event.answer(text, **({"show_alert": True} if callback else {}))
    return False


async def render(route, actor, app_context, state):
    parts = route.split(":")
    view = parts[1]
    admin = app_context.admin
    admin.require_admin(actor)
    if view in {"safety", "safety_confidence"}:
        summary = await admin.safety_summary(actor)
        if view == "safety_confidence":
            return safety_panels.confidence_page(summary)
        shadow = getattr(getattr(app_context, "moderation", None), "shadow", None)
        return safety_panels.dashboard(summary, shadow.runtime_stats() if shadow else None)
    if view == "safety_cases":
        return safety_panels.cases(await admin.page(actor, "safety", int(parts[3]), filter_value=parts[2]), parts[2])
    if view == "safety_case":
        entry = await admin.detail(actor, "safety", int(parts[2]))
        user = await admin.user(actor, entry["telegram_user_id"]) if entry else {}
        return safety_panels.case_card(entry, user, int(parts[3]) if len(parts) > 3 else 0)
    if view == "safety_context":
        entry = await admin.detail(actor, "safety", int(parts[2]))
        cache = getattr(getattr(app_context, "moderation", None), "review_cache", None)
        sample = cache.get(entry["request_id"], entry["telegram_user_id"], entry["policy_fingerprint"]) if cache and entry else None
        if not sample:
            return "Контекст недоступен: он хранится только в RAM, ограничен по времени и очищается при перезапуске. Решения и метрики сохранены.", keyboard([("⬅️ Оценка", f"adm:safety_case:{parts[2]}")])
        pages = [sample["text"][i:i+2800] for i in range(0, len(sample["text"]), 2800)]
        index = min(max(int(parts[3]), 0), len(pages) - 1)
        buttons = []
        if index:
            buttons.append(("‹", f"adm:safety_context:{entry['id']}:{index-1}"))
        if index + 1 < len(pages):
            buttons.append(("›", f"adm:safety_context:{entry['id']}:{index+1}"))
        text = f"<b>Контекст #{entry['id']} · {index+1}/{len(pages)}</b>\n<pre>{safe(pages[index])}</pre>"
        if sample["has_image"]:
            text += "\nОригинал изображения не сохранён в карточке."
        return text, keyboard(buttons, [("⬅️ Оценка", f"adm:safety_case:{entry['id']}")])
    if view in {"home", "stats"}:
        return panels.dashboard(await admin.dashboard(actor), statistics=view == "stats")
    if view == "users":
        scope, page = parts[2], int(parts[3])
        return panels.users_list(await admin.page(actor, "users", page, filter_value=scope), scope)
    if view == "search_results":
        search = (await state.get_data()).get("admin_search", "")
        return panels.users_list(await admin.page(actor, "users", int(parts[2]), search=search), "all", search)
    if view == "user":
        uid = int(parts[2])
        back = "adm:users:all:0"
        if len(parts) == 5:
            scope, index = parts[3:]
            if scope == "search":
                back = f"adm:search_results:{index}"
            else:
                back = f"adm:{scope}:{index}" if scope in {"app", "event", "appeal"} else f"adm:users:{scope}:{index}"
        return panels.user_card(await admin.user(actor, uid), back)
    if view in {"user_usage", "user_events", "user_audit"}:
        uid, page = int(parts[2]), int(parts[3])
        resource = {"user_usage": "usage", "user_events": "events", "user_audit": "audit"}[view]
        result = await admin.page(actor, resource, page, user_id=uid)
        if view == "user_usage":
            return panels.usage_list(result, uid)
        if view == "user_events":
            return panels.events_list(result, user_id=uid)
        return panels.audit_list(result, uid)
    if view == "events":
        scope, page = parts[2], int(parts[3])
        return panels.events_list(await admin.page(actor, "events", page, filter_value=scope), scope=scope)
    if view == "event":
        event = await admin.detail(actor, "events", int(parts[2]))
        return panels.event_card(event, await admin.user(actor, event["telegram_user_id"]) if event else {})
    if view == "trusted":
        return panels.applications_list(await admin.page(actor, "applications", int(parts[2]), filter_value="pending"))
    if view == "app":
        application = await admin.detail(actor, "applications", int(parts[2]))
        user = await admin.user(actor, application["telegram_user_id"]) if application else {}
        return panels.application_card(application, user, int(parts[3]) if len(parts) > 3 else 0)
    if view == "appeals":
        scope, page = parts[2], int(parts[3])
        return panels.appeals_list(await admin.page(actor, "appeals", page, filter_value=scope), scope)
    if view in {"appeal", "appeal_history"}:
        appeal = await admin.detail(actor, "appeals", int(parts[2]))
        user = await admin.user(actor, appeal["telegram_user_id"]) if appeal else {}
        if view == "appeal_history" and appeal:
            aid, uid = appeal["id"], appeal["telegram_user_id"]
            history = await admin.page(actor, "events", int(parts[3]), user_id=uid)
            return panels.events_list(history, user_id=uid, back=f"adm:appeal:{aid}",
                                      page_route=lambda n: f"adm:appeal_history:{aid}:{n}")
        return panels.appeal_card(appeal, user, int(parts[3]) if len(parts) > 3 else 0)
    if view == "audit":
        return panels.audit_list(await admin.page(actor, "audit", int(parts[2])))
    if view == "audit_entry":
        return panels.audit_card(await admin.detail(actor, "audit", int(parts[2])))
    if view == "models":
        return panels.models_page(int(parts[2]))
    raise ValueError("Unknown panel")


@router.message(Command("admin"))
async def admin_handler(message: Message, app_context: AppContext, state: FSMContext):
    if not await _guard(message, app_context):
        return
    await state.clear()
    text, markup = await render("adm:home", message.from_user.id, app_context, state)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


def _cancel():
    return keyboard([("Отмена", "adm:cancel")])


def _check_flow(data):
    if not data.get("action") or data.get("expires_at", 0) < time.time():
        raise ValueError("Expired action")


async def _reason_prompt(callback, state):
    data = await state.get_data()
    await state.set_state(AdminFlow.reason)
    await edit_menu(callback.message,
                    f"<b>{panels.ACTIONS[data['action']]}</b>\n{safe(data['target_label'])}"
                    "\n\nНапишите краткую причину. Не вставляйте переписку или секреты.", _cancel())


async def _begin(callback, state, app_context):
    parts = callback.data.split(":")
    kind, action, target_id = parts[1], parts[2], int(parts[3])
    payload = {"action": action, "panel_chat_id": callback.message.chat.id,
               "panel_message_id": callback.message.message_id, "expires_at": time.time() + 900}
    if kind in {"event_act", "review"}:
        event = await app_context.admin.detail(callback.from_user.id, "events", target_id)
        payload.update(user_id=event["telegram_user_id"], event_id=target_id, back=f"adm:event:{target_id}")
        if kind == "review":
            if action not in {"open", "resolved", "dismissed"}:
                raise ValueError("Unknown review status")
            payload.update(action="review_moderation", review_status=action)
    elif kind == "app_act":
        if action not in {"approve_trusted", "reject_trusted"}:
            raise ValueError("Unknown application action")
        application = await app_context.admin.detail(callback.from_user.id, "applications", target_id)
        if application.get("status") != "pending":
            raise ValueError("Already decided")
        payload.update(user_id=application["telegram_user_id"], application_id=target_id, back=f"adm:app:{target_id}")
    elif kind == "appeal_act":
        if action not in {"approve_appeal", "reject_appeal"}:
            raise ValueError("Unknown appeal action")
        appeal = await app_context.admin.detail(callback.from_user.id, "appeals", target_id)
        if appeal.get("status") != "pending":
            raise ValueError("Already decided")
        payload.update(user_id=appeal["telegram_user_id"], appeal_id=target_id, back=f"adm:appeal:{target_id}")
    elif kind == "safety_review":
        if action not in {"allow", "safe_complete", "refuse", "violation", "unclear"}:
            raise ValueError("Invalid human safety label")
        entry = await app_context.admin.detail(callback.from_user.id, "safety", target_id)
        if entry.get("status") != "completed":
            raise ValueError("Completed safety evaluation is required")
        payload.update(action="review_safety", user_id=entry["telegram_user_id"], safety_evaluation_id=target_id,
                       expected_safety_action=action, expected_safety_review_id=entry["review_admin_action_id"], back=f"adm:safety_case:{target_id}")
    else:
        if action not in {"block", "unblock", "restrict", "unrestrict", "warn", "clear_warnings",
                          "set_limit", "grant_trusted", "revoke_trusted", "grant_grok", "revoke_grok",
                          "grant_premium", "revoke_premium"}:
            raise ValueError("Unknown action")
        payload.update(user_id=target_id, back=f"adm:user:{target_id}")
    if payload["action"] not in panels.ACTIONS:
        raise ValueError("Unknown action")
    user = await app_context.admin.user(callback.from_user.id, payload["user_id"])
    if not user:
        raise ValueError("User not found")
    payload["target_label"] = f"{user_label(user)} · {payload['user_id']}"
    if kind == "safety_review":
        payload["target_label"] += f" · Верное действие: {action.upper()}"
    await state.set_data(payload)
    if action == "restrict":
        await state.set_state(AdminFlow.duration)
        await edit_menu(callback.message, "<b>⏳ Временное ограничение</b>\n\nВыберите срок:", keyboard(
            [("15 минут", "adm:duration:15"), ("1 час", "adm:duration:60")],
            [("24 часа", "adm:duration:1440"), ("7 дней", "adm:duration:10080")],
            [("Другой срок", "adm:duration:custom")], [("Отмена", "adm:cancel")]))
    elif action == "set_limit":
        await state.set_state(AdminFlow.limit)
        await edit_menu(callback.message, "<b>🎚 Дневной лимит AI-вызовов</b>\n\nВыберите значение:", keyboard(
            [("50", "adm:limit:50"), ("100", "adm:limit:100"), ("500", "adm:limit:500")],
            [("По тарифу", "adm:limit:default"), ("Без лимита", "adm:limit:off")],
            [("Другое значение", "adm:limit:custom")], [("Отмена", "adm:cancel")]))
    else:
        await _reason_prompt(callback, state)


@router.callback_query(F.data.startswith("adm:"))
async def admin_callback(callback: CallbackQuery, app_context: AppContext, state: FSMContext):
    if not await _guard(callback, app_context):
        return
    try:
        parts = callback.data.split(":")
        command = parts[1]
        if command in {"act", "event_act", "review", "app_act", "appeal_act", "safety_review"}:
            await _begin(callback, state, app_context)
        elif command == "safety_switch":
            if parts[2] not in {"v4", "v5"}:
                raise ValueError("Invalid policy version")
            summary = await app_context.admin.safety_summary(callback.from_user.id)
            if parts[2] == "v5" and not summary["readiness"]["ready"]:
                raise ValueError("Shadow evaluation is incomplete")
            await state.set_data({"action": "activate_safety_v5" if parts[2] == "v5" else "rollback_safety_v4",
                "user_id": callback.from_user.id, "target_label": f"Production policy → {parts[2]}",
                "panel_chat_id": callback.message.chat.id, "panel_message_id": callback.message.message_id,
                "back": "adm:safety", "expires_at": time.time() + 900})
            await _reason_prompt(callback, state)
        elif command in {"duration", "limit"}:
            data = await state.get_data()
            _check_flow(data)
            expected = AdminFlow.duration.state if command == "duration" else AdminFlow.limit.state
            if await state.get_state() != expected:
                raise ValueError("Expired choice")
            value = parts[2]
            if value == "custom":
                prompt = "Введите срок в минутах (1–525600)." if command == "duration" else "Введите число AI-вызовов в сутки (1–1000000)."
                await edit_menu(callback.message, prompt, _cancel())
            else:
                if command == "duration":
                    minutes = int(value)
                    if not 1 <= minutes <= 525600:
                        raise ValueError("Invalid duration")
                    await state.update_data(minutes=minutes)
                elif value == "default":
                    await state.update_data(action="reset_limit")
                else:
                    limit = None if value == "off" else int(value)
                    if limit is not None and not 1 <= limit <= 1_000_000:
                        raise ValueError("Invalid limit")
                    await state.update_data(daily_request_limit=limit)
                await _reason_prompt(callback, state)
        elif command == "confirm":
            data = await state.get_data()
            _check_flow(data)
            if await state.get_state() != AdminFlow.confirm.state or not secrets.compare_digest(parts[2], data.get("nonce", "")):
                raise ValueError("Expired confirmation")
            # Consume before mutation. Dispatcher isolates events per user; an
            # old/double confirmation cannot perform the action twice.
            await state.clear()
            options = {key: data[key] for key in ("user_id", "event_id", "application_id", "appeal_id", "minutes", "daily_request_limit", "review_status", "safety_evaluation_id", "expected_safety_action", "expected_safety_review_id") if key in data}
            result = await app_context.admin.act(callback.from_user.id, data["action"], reason=data["reason"], **options)
            app_context.notifications.wake()
            text, markup = await render(data["back"], callback.from_user.id, app_context, state)
            await edit_menu(callback.message, f"✅ Готово · Audit #{result.audit_id}\n\n" + text, markup)
        elif command == "cancel":
            data = await state.get_data()
            await state.clear()
            text, markup = await render(data.get("back", "adm:home"), callback.from_user.id, app_context, state)
            await edit_menu(callback.message, text, markup)
        elif command == "search":
            await state.set_data({"panel_chat_id": callback.message.chat.id, "panel_message_id": callback.message.message_id})
            await state.set_state(AdminFlow.search)
            await edit_menu(callback.message, "<b>🔎 Поиск пользователя</b>\n\nВведите имя или @username. Также можно указать Telegram ID.", _cancel())
        else:
            search = (await state.get_data()).get("admin_search", "")
            await state.clear()
            if search:
                await state.update_data(admin_search=search)
            text, markup = await render(callback.data, callback.from_user.id, app_context, state)
            await edit_menu(callback.message, text, markup)
        await callback.answer()
    except (ValueError, KeyError, IndexError):
        await state.clear()
        await callback.answer("Действие недоступно или кнопка устарела. Откройте панель заново.", show_alert=True)


@router.message(AdminFlow.reason, F.text)
async def admin_reason(message: Message, app_context: AppContext, state: FSMContext):
    if not await _guard(message, app_context):
        return
    data = await state.get_data()
    try:
        _check_flow(data)
    except ValueError:
        await state.clear()
        await message.answer("Диалог истёк. Откройте /admin заново.")
        return
    reason = app_context.admin.clean_reason(message.text)
    if not reason:
        await message.answer("Укажите краткую причину.")
        return
    await delete_private_input(message)
    nonce = secrets.token_hex(6)
    await state.update_data(reason=reason, nonce=nonce)
    await state.set_state(AdminFlow.confirm)
    text = f"<b>Подтвердите действие</b>\n\n{panels.ACTIONS[data['action']]}\n{safe(data['target_label'])}"
    if "minutes" in data:
        text += f"\nСрок: {data['minutes']} мин."
    if "daily_request_limit" in data:
        text += f"\nЛимит: {data['daily_request_limit'] if data['daily_request_limit'] is not None else 'без лимита'}"
    if "review_status" in data:
        text += f"\nРезультат: {panels.STATUSES[data['review_status']]}"
    text += f"\n\nПричина: {safe(reason)}"
    if data["user_id"] == message.from_user.id and data["action"] in {"block", "restrict"}:
        text += "\n\n⚠️ Это ограничит ваш собственный доступ к боту."
    await edit_saved(message.bot, data, text, keyboard([("✅ Подтвердить", f"adm:confirm:{nonce}"), ("Отмена", "adm:cancel")]))


@router.message(AdminFlow.duration, F.text)
@router.message(AdminFlow.limit, F.text)
async def admin_number(message: Message, app_context: AppContext, state: FSMContext):
    if not await _guard(message, app_context):
        return
    data = await state.get_data()
    try:
        _check_flow(data)
        duration = await state.get_state() == AdminFlow.duration.state
        value = int(message.text)
        if not 1 <= value <= (525600 if duration else 1_000_000):
            raise ValueError("Invalid number")
    except ValueError:
        await message.answer("Введите допустимое положительное число или нажмите «Отмена».")
        return
    await state.update_data(**({"minutes": value} if duration else {"daily_request_limit": value}))
    await state.set_state(AdminFlow.reason)
    await delete_private_input(message)
    await edit_saved(message.bot, data, "<b>Причина действия</b>\n\nНапишите краткое основание.", _cancel())


@router.message(AdminFlow.search, F.text)
async def admin_search(message: Message, app_context: AppContext, state: FSMContext):
    if not await _guard(message, app_context):
        return
    data = await state.get_data()
    search = message.text.strip()[:80]
    await state.clear()
    await state.update_data(admin_search=search)
    text, markup = await render("adm:search_results:0", message.from_user.id, app_context, state)
    await edit_saved(message.bot, data, text, markup)
    await delete_private_input(message)


@router.message(AdminFlow.reason)
@router.message(AdminFlow.duration)
@router.message(AdminFlow.limit)
@router.message(AdminFlow.search)
@router.message(AdminFlow.confirm)
async def admin_flow_fallback(message: Message, app_context: AppContext):
    if await _guard(message, app_context):
        await message.answer("Завершите действие в панели или нажмите «Отмена».", reply_markup=_cancel())
