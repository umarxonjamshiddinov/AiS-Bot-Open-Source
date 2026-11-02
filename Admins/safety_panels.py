"""Safety views inside the existing administrator panel."""
from Handlers.ui import keyboard, pager, safe, date_label, user_label


def dashboard(data, runtime=None):
    ready, policy = data["readiness"], data["policy"]
    labels = {"allow": "ALLOW", "safe_complete": "SAFE_COMPLETE", "refuse": "REFUSE", "violation": "VIOLATION"}
    lines = ["<b>🛡 AiS Adaptive Safety</b>", f"Production: <b>{safe(policy['policy_version'])}</b>",
             "v5: ais_safety_v2 · confidence — самооценка модели, не вероятность ошибки.",
             "\n<b>Последние 24 часа</b>"]
    for mode in ("shadow", "production"):
        counts = {r["action"]: r["count"] for r in data["actions"] if r["mode"] == mode}
        lines.append(f"{mode}: " + " · ".join(f"{label} {counts.get(key, 0)}" for key, label in labels.items()))
    errors = sum(row["count"] for row in data["status"] if row["status"] == "error")
    lines.append(f"Ошибки проверок: production {data['v4_errors']} · v5 {errors}")
    lines.append(f"Повторные попытки JSON-контракта: {data['contract_retries']}")
    lat = data["latency"]
    lines.append(f"v5 latency: p50 {lat['p50'] if lat['p50'] is not None else '—'} ms · p95 {lat['p95'] if lat['p95'] is not None else '—'} ms")
    w = data["warnings"]
    lines.extend([f"Auto-blocks production: {data['auto_blocks']}", f"Отмены по успешным апелляциям: {data['appeal_overturns']}",
                  f"Warnings за период: {w['issued']} · отменено {w['revoked']} ({w['revoked_percent'] if w['revoked_percent'] is not None else '—'}%)",
                  f"Известные shadow tokens: {data['shadow_tokens'] if data['shadow_tokens'] is not None else '—'}"])
    for title, rows, key in (("Категории v5", data["categories"], "category"), ("Причины policy", data["reasons"], "reason_code")):
        lines.append(f"\n<b>{title}</b>")
        lines.extend(f"{safe(row[key])}: {row['count']}" for row in rows)
        if not rows:
            lines.append("Пока нет данных.")
    lines.extend(["\n<b>Готовность к ручному включению v5</b>",
        f"Реальные сравнимые запросы: {ready['comparable']}/{ready['minimum_samples']}",
        f"Расхождения: {ready['disagreements']} · не разобрано: {ready['unreviewed']}",
        f"Ручных оценок: {ready['reviewed']} · подтверждённых ошибок v5: {ready['known_errors']}",
        f"Ошибки вызова/контракта: {round(100 * ready['error_rate'], 1) if ready['error_rate'] is not None else '—'}% (порог ≤2%)",
        "Автоматического переключения нет. Старые fingerprints в выборку не входят."])
    if runtime:
        lines.append(f"Очередь: {runtime['queued']} · пропущено с запуска: {runtime['dropped_since_start']} · ошибок записи: {runtime['storage_errors_since_start']}")
    buttons = [[("🔎 Расхождения", "adm:safety_cases:pending:0"), ("📋 Все оценки", "adm:safety_cases:all:0")],
               [("❌ Ошибки v5", "adm:safety_cases:errors:0"), ("📐 Confidence", "adm:safety_confidence")]]
    if policy["policy_version"] == "ais_safety_v1" and ready["ready"]:
        buttons.append([("🚀 Включить v5", "adm:safety_switch:v5")])
    if policy["policy_version"] == "ais_safety_v2":
        buttons.append([("↩️ Вернуть v4", "adm:safety_switch:v4")])
    buttons.append([("🔄 Обновить", "adm:safety"), ("⬅️ Панель", "adm:home")])
    return "\n".join(lines), keyboard(*buttons)


def confidence_page(data):
    lines = ["<b>📐 Confidence routing</b>", "Высокая: ≥0.90; средняя: 0.60–0.89; низкая: &lt;0.60.",
             "Warning требует также прямого серьёзного нарушения и установленного намерения.",
             "\nЭто самооценка LLM. Доли ниже относятся только к вручную размеченной выборке за 24 часа, не ко всем запросам."]
    for row in data["confidence_review"]:
        lines.append(f"\n{row['band']}: совпало {row['matched']} из {row['reviewed']} ручных оценок")
    if not data["confidence_review"]:
        lines.append("\nРучной разметки пока нет. Эмпирическая точность неизвестна.")
    return "\n".join(lines), keyboard([("⬅️ Safety", "adm:safety")])


def cases(page, scope):
    rows = [[(f"#{e['id']} · {e['production_action'] or '—'} → {e['action'] or e['status']}", f"adm:safety_case:{e['id']}:{page.page}")] for e in page.items]
    rows.extend([pager(page, lambda n: f"adm:safety_cases:{scope}:{n}"),
                 [("Не разобраны", "adm:safety_cases:pending:0"), ("Все расхождения", "adm:safety_cases:disagreements:0")],
                 [("⬅️ Safety", "adm:safety")]])
    return f"<b>🔎 Safety · {safe(scope)}</b> · {page.total}\n\n" + ("Выберите оценку." if page.items else "Оценок нет."), keyboard(*rows)


def case_card(entry, user, page=0):
    if not entry:
        return "Оценка не найдена.", keyboard([("⬅️ Safety", "adm:safety")])
    eid, uid = entry["id"], entry["telegram_user_id"]
    text = (f"<b>🛡 Safety #{eid} · {safe(entry['mode'])}</b>\n{safe(user_label(user))} · <code>{uid}</code>"
            f"\n{date_label(entry['created_at'])}\nPolicy: {safe(entry['policy_version'])} · <code>{entry['policy_fingerprint'][:12]}</code>"
            f"\nОсновная модель: {safe(entry['selected_model'])} / {safe(entry['selected_provider'])}"
            f"\nClassifier: {safe(entry['classifier_model'])}\nСтатус: {safe(entry['status'])}"
            f"\n\nProduction: <b>{safe(entry['production_action'])}</b>\nv5 policy: <b>{safe(entry['action'])}</b>"
            f"\nСовет LLM: {safe(entry['model_decision'])} / {safe(entry['recommended_action'])}"
            f"\nIntent: {safe(entry['intent'])}\nContext: {safe(entry['request_context'])}"
            f"\nAuthorization: {safe(entry['authorization'])}\nКатегория: {safe(entry['category'])}"
            f"\nSeverity: {safe(entry['severity'])}\nConfidence: {safe(entry['confidence'])} ({safe(entry['confidence_source'])})"
            f"\nRule: {safe(entry['reason_code'])}\nClassifier reason: {safe(entry['classifier_reason_code'])}"
            f"\nLatency: {safe(entry['latency_ms'])} ms")
    if entry["mode"] == "shadow":
        text += "\n\nЭта shadow-оценка не применяла sanctions и не меняла ответ пользователю."
    if entry["error_type"]:
        text += f"\nОшибка: {safe(entry['error_type'])} / {safe(entry['error_code'])}"
    if entry["expected_action"]:
        text += (f"\n\nРучная оценка: <b>{safe(entry['expected_action'])}</b>"
                 f"\nReviewer: <code>{entry['reviewed_by']}</code> · {date_label(entry['reviewed_at'])}"
                 f"\nПричина: {safe(entry['admin_reason'])}")
    buttons = [[("📋 Контекст из RAM", f"adm:safety_context:{eid}:0")]]
    if entry["status"] == "completed":
        buttons.extend([[("Верно ALLOW", f"adm:safety_review:allow:{eid}"), ("Верно SAFE_COMPLETE", f"adm:safety_review:safe_complete:{eid}")],
                        [("Верно REFUSE", f"adm:safety_review:refuse:{eid}"), ("Верно VIOLATION", f"adm:safety_review:violation:{eid}")],
                        [("Недостаточно данных", f"adm:safety_review:unclear:{eid}")]])
    buttons.extend([[("👤 Пользователь", f"adm:user:{uid}")], [("⬅️ Список", f"adm:safety_cases:all:{page}")]])
    return text, keyboard(*buttons)
