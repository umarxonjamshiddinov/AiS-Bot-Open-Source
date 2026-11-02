"""Presentation only: repositories and mutations live behind AdminService."""
from AI.models import user_models
from AI.access import requires_premium
from Handlers.ui import date_label, keyboard, limit_label, pager, safe, user_label
from Storage.models import Page


ACTIONS = {
    "block": "🚫 Заблокировать", "unblock": "✅ Разблокировать", "restrict": "⏳ Ограничить",
    "unrestrict": "🔓 Снять ограничение", "warn": "⚠️ Предупреждение", "clear_warnings": "🧹 Очистить ручные предупреждения",
    "grant_trusted": "⭐ Выдать Trusted", "revoke_trusted": "❌ Забрать Trusted",
    "grant_grok": "🤖 Grant Premium", "revoke_grok": "🔒 Revoke Premium",
    "grant_premium": "🤖 Grant Premium", "revoke_premium": "🔒 Revoke Premium",
    "set_limit": "🎚 Изменить лимит", "reset_limit": "🎚 Лимит тарифа",
    "approve_trusted": "✅ Одобрить Trusted", "reject_trusted": "❌ Отклонить заявку",
    "review_moderation": "⚠️ Рассмотреть событие",
    "approve_appeal": "✅ Удовлетворить апелляцию", "reject_appeal": "❌ Отклонить апелляцию",
    "review_safety": "🔎 Разметить safety-решение", "activate_safety_v5": "🚀 Включить Safety v5",
    "rollback_safety_v4": "↩️ Вернуть Safety v4",
}
STATUSES = {"pending": "ожидает", "approved": "одобрена", "rejected": "отклонена", "authorized": "активен",
            "revoked": "доступ отозван", "succeeded": "готово", "error": "ошибка", "cancelled": "отменён",
            "interrupted": "прерван", "stale": "контекст сменился", "blocked": "заблокирован",
            "restricted": "ограничен", "unauthorized": "доступ закрыт", "limit_exceeded": "лимит исчерпан",
            "open": "не рассмотрено", "resolved": "рассмотрено", "dismissed": "отклонено"}


def dashboard(data, *, statistics=False):
    today = data["today"]
    text = ("<b>📊 Статистика AiS</b>" if statistics else "<b>🛠 AiS · Админ-панель</b>") + (
        f"\n\n👥 Пользователей: <b>{data['users']}</b>\n⭐ Trusted: {data['trusted']}"
        f"\n🚫 Заблокированы: {data['blocked']}\n🤖 Premium Models: {data['premium']}"
        f"\n\n<b>Сегодня · UTC</b>\nAI-вызовы: {today['inferences']}"
        f"\nИзвестные токены: {today['known_tokens'] if today['known_tokens'] is not None else '—'}"
        f"\nОшибки upstream: {today['errors']}\n\n📨 Заявки Trusted: {data['pending_applications']}"
        f"\n📨 Апелляции: {data['pending_appeals']}"
        f"\n⚠️ События к рассмотрению: {data['open_events']}")
    return text, keyboard(
        [("📊 Статистика", "adm:stats"), ("👥 Пользователи", "adm:users:all:0")],
        [("🚫 Заблокированные", "adm:users:blocked:0"), ("⚠️ Модерация", "adm:events:open:0")],
        [("⭐ Trusted", "adm:trusted:0"), ("🤖 Модели", "adm:models:0")],
        [("📨 Апелляции", "adm:appeals:pending:0")],
        [("🛡 Safety", "adm:safety")],
        [("📋 Audit Log", "adm:audit:0"), ("🔄 Обновить", "adm:home")],
        [("⬅️ В меню", "main_menu")])


def user_card(user, back="adm:users:all:0"):
    if not user:
        return "Пользователь не найден.", keyboard([("⬅️ Назад", back)])
    uid = user["telegram_user_id"]
    tier = "⭐ Trusted" if user["tier"] == "TRUSTED" else "Standard"
    moderation = ", ".join(f"{row['action']}: {row['events']}" for row in user["moderation"]) or "нет событий"
    requests = sum(row["requests"] for row in user["usage"])
    restriction = user.get("restriction_until")
    text = (f"<b>👤 {safe(user_label(user))}</b>\nTelegram ID: <code>{uid}</code>"
            f"\nUsername: {safe('@' + user['username'] if user['username'] else None)}"
            f"\nИмя: {safe(user['first_name'])} {safe(user['last_name'])}"
            f"\nРегистрация: {date_label(user['created_at'])}\nПоследний визит: {date_label(user['last_seen_at'])}"
            f"\n\nСтатус: {STATUSES.get(user['status'], '—')} · <b>{tier}</b>"
            f"\nИндивидуальный лимит: {limit_label(user['daily_request_limit']) if user['has_limit_override'] else 'по тарифу'}"
            f"\nДействующий лимит: {limit_label(user['effective_daily_limit'])}"
            f"\nAI-вызовы сегодня: <b>{user['requests_today']}</b>\nПредупреждения: {user['active_warnings']}"
            f"\nАктивные moderation warnings: <b>{user['active_moderation_warnings']}/3</b>"
            f"\nОграничение до: {date_label(restriction)}\nБлокировка: {'да' if user['is_blocked'] else 'нет'}"
            f"\nРучная / legacy блокировка: {'да' if user['manual_blocked'] else 'нет'}"
            f"\nАвтоматическая блокировка: {'да' if user['automatic_block'] else 'нет'}"
            f"\nHigher-cost сегодня: {user['higher_cost_requests_today']}/{user['higher_cost_daily_limit']}"
            f"\nPremium Models: {'✅ доступны' if user['premium_models_allowed'] else '🔒 закрыты'}"
            f"\n\nЗапросов в сохранённой статистике: {requests}\nМодерация: {safe(moderation)}")
    rows = []
    for actions in (("unblock" if user["manual_blocked"] else "block", "restrict"),
                    ("warn", "clear_warnings"),
                    ("revoke_trusted" if user["tier"] == "TRUSTED" else "grant_trusted",
                     "revoke_premium" if user["premium_models_allowed"] else "grant_premium")):
        rows.append([(ACTIONS[action], f"adm:act:{action}:{uid}") for action in actions])
    if restriction:
        rows.append([(ACTIONS["unrestrict"], f"adm:act:unrestrict:{uid}")])
    rows.extend([
        [("🎚 Лимит", f"adm:act:set_limit:{uid}"), ("📊 Usage", f"adm:user_usage:{uid}:0")],
        [("⚠️ Events", f"adm:user_events:{uid}:0"), ("📋 Audit", f"adm:user_audit:{uid}:0")],
        [("⬅️ Назад", back), ("🔄 Обновить", f"adm:user:{uid}")]])
    return text, keyboard(*rows)


def users_list(page, scope, search=""):
    titles = {"all": "👥 Пользователи", "blocked": "🚫 Заблокированные", "trusted": "⭐ Trusted", "grok": "🤖 Premium Models", "premium": "🤖 Premium Models"}
    rows = [[(f"{'⭐' if u['tier'] == 'TRUSTED' else '👤'} {user_label(u)} · {u['telegram_user_id']}",
               f"adm:user:{u['telegram_user_id']}:{'search' if search else scope}:{page.page}")] for u in page.items]
    route = (lambda n: f"adm:search_results:{n}") if search else (lambda n: f"adm:users:{scope}:{n}")
    rows.extend([pager(page, route), [("🔎 Найти пользователя", "adm:search")], [("⬅️ Панель", "adm:home")]])
    return f"<b>{titles[scope]}</b> · {page.total}" + (f"\nПоиск: {safe(search)}" if search else "") + ("\n\nСписок пуст." if not page.items else "\n\nВыберите пользователя:"), keyboard(*rows)


def events_list(page, *, user_id=None, scope="open", back=None, page_route=None):
    rows = [[(f"⚠️ #{e['id']} · {user_label(e)} · {e['action']}", f"adm:event:{e['id']}")] for e in page.items]
    route = (lambda n: f"adm:user_events:{user_id}:{n}") if user_id else (lambda n: f"adm:events:{scope}:{n}")
    rows.append(pager(page, page_route or route))
    if user_id is None:
        rows.append([("Все события", "adm:events:all:0"), ("К рассмотрению", "adm:events:open:0")])
    rows.append([("⬅️ Назад", back or (f"adm:user:{user_id}" if user_id else "adm:home"))])
    return f"<b>⚠️ Модерация</b> · {page.total}\n\n" + ("Выберите событие." if page.items else "Событий нет."), keyboard(*rows)


def event_card(event, user):
    if not event:
        return "Событие не найдено.", keyboard([("⬅️ Назад", "adm:events:open:0")])
    eid, uid = event["id"], event["telegram_user_id"]
    text = (f"<b>⚠️ Событие #{eid}</b>\n{safe(user_label(user))} · <code>{uid}</code>"
            f"\nДата: {date_label(event['timestamp'])}\nДействие: {safe(event['action'])}"
            f"\nМодель: {safe(event['model'])}\nProvider: {safe(event['provider'])}"
            f"\nКатегория: {safe(event['category'])}\nСерьёзность: {safe(event['severity'])}"
            f"\nИсточник: {safe(event['source'])}\nОснование: {safe(event['code'])}"
            f"\nПроверка: {safe(event['guardrail'])}\nСтатус: {STATUSES[event['review_status']]}"
            "\n\nСодержимое сообщений не сохраняется.")
    if event.get("automatic_action", "none") != "none":
        text += (f"\n\nАвтоматическое действие: {safe(event['automatic_action'])}"
                 f"\nWarning #{event['warning_id']} · {event['active_warning_count']}/3 на момент решения"
                 f"\nИсточник сигнала: {safe(event['signal_source'])}")
        return text, keyboard([("👤 Пользователь", f"adm:user:{uid}:event:{eid}")],
                              [("📨 Апелляции", "adm:appeals:pending:0")], [("⬅️ События", "adm:events:all:0")])
    return text, keyboard(
        [("✅ Рассмотрено", f"adm:review:resolved:{eid}"), ("❌ Отклонить", f"adm:review:dismissed:{eid}")],
        [("↩️ Вернуть в работу", f"adm:review:open:{eid}")],
        [("🚫 Заблокировать", f"adm:event_act:block:{eid}"), ("⚠️ Предупредить", f"adm:event_act:warn:{eid}")],
        [("👤 Пользователь", f"adm:user:{uid}:event:{eid}")], [("⬅️ События", "adm:events:open:0")])


def applications_list(page):
    rows = [[(f"📨 {user_label(a)} · {a['telegram_user_id']}", f"adm:app:{a['id']}:{page.page}")] for a in page.items]
    rows.extend([pager(page, lambda n: f"adm:trusted:{n}"), [("⭐ Пользователи Trusted", "adm:users:trusted:0")], [("⬅️ Панель", "adm:home")]])
    return f"<b>⭐ Trusted · Заявки</b> · {page.total}\n\n" + ("Выберите заявку." if page.items else "Новых заявок нет."), keyboard(*rows)


def application_card(application, user, page=0):
    if not application:
        return "Заявка не найдена.", keyboard([("⬅️ Заявки", "adm:trusted:0")])
    aid, uid = application["id"], application["telegram_user_id"]
    text = (f"<b>📨 Заявка Trusted #{aid}</b>\n{safe(user_label(user))} · <code>{uid}</code>"
            f"\nИмя: {safe(user['first_name'])} {safe(user['last_name'])}"
            f"\nРегистрация: {date_label(user['created_at'])}\nЗаявка: {date_label(application['created_at'])}"
            f"\nСтатус: <b>{STATUSES[application['status']]}</b>"
            f"\n\nAI-вызовы сегодня: {user['requests_today']}\nПредупреждения: {user['active_warnings']}"
            f"\nСобытия модерации: {sum(e['events'] for e in user['moderation'])}"
            f"\nБлокировка: {'да' if user['is_blocked'] else 'нет'}")
    rows = []
    if application["status"] == "pending":
        rows.append([("✅ Одобрить", f"adm:app_act:approve_trusted:{aid}"), ("❌ Отклонить", f"adm:app_act:reject_trusted:{aid}")])
    rows.extend([[("👤 Профиль и usage", f"adm:user:{uid}:app:{aid}")], [("⬅️ Заявки", f"adm:trusted:{page}")]])
    return text, keyboard(*rows)


def appeals_list(page, scope="pending"):
    rows = [[(f"📨 #{a['id']} · {user_label(a)}", f"adm:appeal:{a['id']}:{page.page}")] for a in page.items]
    rows.extend([pager(page, lambda n: f"adm:appeals:{scope}:{n}"),
                 [("Ожидают решения", "adm:appeals:pending:0"), ("Все апелляции", "adm:appeals:all:0")],
                 [("⬅️ Панель", "adm:home")]])
    return (f"<b>📨 Апелляции · {'ожидают решения' if scope == 'pending' else 'все'}</b> · {page.total}\n\n"
            + ("Выберите апелляцию." if page.items else "Апелляций нет.")), keyboard(*rows)


def appeal_card(appeal, user, page=0):
    if not appeal:
        return "Апелляция не найдена.", keyboard([("⬅️ Назад", "adm:appeals:pending:0")])
    aid, uid, event = appeal["id"], appeal["telegram_user_id"], appeal["event"]
    text = (f"<b>📨 Апелляция #{aid}</b>\n{safe(user_label(user))} · Telegram ID: <code>{uid}</code>"
            f"\nUsername: {safe('@' + user['username'] if user['username'] else None)}"
            f"\nИмя: {safe(user['first_name'])} {safe(user['last_name'])}"
            f"\nПодана: {date_label(appeal['created_at'])}\nСтатус: <b>{STATUSES[appeal['status']]}</b>"
            f"\n\nModeration event #{event['id']} · {date_label(event['timestamp'])}"
            f"\nМодель: {safe(event['model'])}\nProvider: {safe(event['provider'])}"
            f"\nИсточник: {safe(event['signal_source'] or event['source'])}")
    for label, value in (("Категория", event["category"]), ("Серьёзность", event["provider_severity"] or event["severity"]),
                         ("Code", event["provider_code"] or event["code"]), ("Confidence", event["confidence"])):
        if value is not None and value != "unknown":
            text += f"\n{label}: {safe(value)}"
    if event.get("classifier_model"):
        text += (f"\nПроверяющая модель: {safe(event['classifier_model'])}"
                 f"\nProvider проверки: {safe(event['classifier_provider'])}"
                 f"\nВерсия policy: {safe(event['policy_version'])}")
    text += (f"\nWarning #{appeal['warning_id']} · Block #{appeal['sanction_id']}"
             f"\nАктивные warnings сейчас: <b>{user['active_moderation_warnings']}/3</b>"
             f"\nНа момент события: {event['active_warning_count']}/3"
             "\nПричина automatic block: 3 активных moderation warnings."
             "\n\n<b>Последние события модерации</b>")
    for entry in appeal["history"]:
        text += f"\n#{entry['id']} · {safe(entry['code'])} · {STATUSES[entry['review_status']]}"
    text += f"\n\n<b>Объяснение пользователя</b>\n{safe(appeal['explanation'])}"
    if appeal["reviewed_at"]:
        text += (f"\n\nРассмотрел: <code>{appeal['reviewed_by']}</code> · {date_label(appeal['reviewed_at'])}"
                 f"\nПричина: {safe(appeal['admin_reason'])}")
    rows = []
    if appeal["status"] == "pending":
        rows.append([("✅ Удовлетворить", f"adm:appeal_act:approve_appeal:{aid}"),
                     ("❌ Отклонить", f"adm:appeal_act:reject_appeal:{aid}")])
    rows.extend([[("👤 Пользователь", f"adm:user:{uid}:appeal:{aid}")],
                 [("📋 История модерации", f"adm:appeal_history:{aid}:0")],
                 [("⬅️ Назад", f"adm:appeals:pending:{page}")]])
    return text, keyboard(*rows)


def audit_list(page, user_id=None):
    rows = [[(f"#{a['id']} · {ACTIONS.get(a['action'], 'Действие')} · {user_label(a)}", f"adm:audit_entry:{a['id']}")] for a in page.items]
    route = (lambda n: f"adm:user_audit:{user_id}:{n}") if user_id else (lambda n: f"adm:audit:{n}")
    rows.extend([pager(page, route), [("⬅️ Назад", f"adm:user:{user_id}" if user_id else "adm:home")]])
    return f"<b>📋 Audit Log</b> · {page.total}\n\n" + ("Выберите запись." if page.items else "Записей нет."), keyboard(*rows)


def audit_card(entry):
    if not entry:
        return "Запись не найдена.", keyboard([("⬅️ Audit", "adm:audit:0")])
    return (f"<b>📋 Audit #{entry['id']}</b>\n{ACTIONS.get(entry['action'], 'Административное действие')}"
            f"\nАдминистратор: <code>{entry['admin_id']}</code>\nПользователь: <code>{entry['target_user_id']}</code>"
            f"\nДата: {date_label(entry['timestamp'])}\n\nПричина: {safe(entry['reason'])}"), keyboard(
                [("👤 Пользователь", f"adm:user:{entry['target_user_id']}")], [("⬅️ Audit", "adm:audit:0")])


def model_name(key):
    from AI.models import MODEL_REGISTRY
    model = MODEL_REGISTRY.get(key)
    return model.display_name if model else "Модель недоступна"


def usage_list(page, user_id):
    lines = [f"<b>📊 Usage</b> · {page.total} запросов", "Токены указаны только при наличии данных upstream."]
    for item in page.items:
        tokens = item["accounted_tokens"]
        lines.append(f"\n<b>{safe(model_name(item['model']))}</b> · {STATUSES.get(item['status'], '—')}"
                     f"\n{date_label(item['timestamp'])}\nИзвестные токены: {tokens if tokens is not None else '—'}"
                     + (" (частично)" if tokens is not None and not item["usage_complete"] else ""))
    return "\n".join(lines), keyboard(pager(page, lambda n: f"adm:user_usage:{user_id}:{n}"), [("⬅️ Пользователь", f"adm:user:{user_id}")])


def models_page(index):
    models = list(user_models())
    index = min(max(index, 0), max(0, (len(models) - 1) // 10))
    page = Page([], index, len(models), 10)
    lines = ["<b>🤖 Модели AiS</b>", "Premium Models access выдаётся в карточке пользователя. Higher-cost: общий дневной pool."]
    lines.extend(f"\n{'🔒' if requires_premium(m) else '•'} <b>{safe(m.display_name)}</b>\n{safe(m.provider)} · {m.cost_tier.value} · "
                 + ("текст и фото" if m.supports_vision else "текст; фото через помощника") for m in models[index * 10:(index + 1) * 10])
    return "\n".join(lines), keyboard(pager(page, lambda n: f"adm:models:{n}"), [("⬅️ Панель", "adm:home")])
