from aiogram.utils.keyboard import InlineKeyboardBuilder

from AI.providers import available_providers, creators_for_backend, models_for_creator
from AI.access import requires_premium


def main_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="🤖 Модели", callback_data="models")
    builder.button(text="💬 Новый чат", callback_data="new_chat")
    builder.button(text="⭐ Trusted", callback_data="acct:trusted")
    builder.button(text="🔓 Premium Models", callback_data="acct:premium_models")
    builder.button(text="👤 Мой статус", callback_data="acct:status")
    builder.button(text="✉️ Поддержка", callback_data="support")
    builder.adjust(2)
    return builder.as_markup()


def providers_menu(settings=None):
    builder = InlineKeyboardBuilder()
    builder.button(text="🎲 Random Model", callback_data="random_model")
    for key, name in available_providers():
        builder.button(text=name, callback_data=f"provider:{key}")
    builder.button(text="⬅️ В меню", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()


def provider_creators_menu(provider: str, settings=None, *, premium_models_allowed=None, grok_allowed=False):
    builder = InlineKeyboardBuilder()
    builder.button(text="🎲 Random Model", callback_data="random_model")
    for creator in creators_for_backend(provider):
        label = "Grok" if creator.casefold() == "xai" else creator
        builder.button(text=label, callback_data=f"creator:{provider}:{creator}")
    builder.button(text="⬅️ Провайдеры", callback_data="models")
    builder.adjust(1)
    return builder.as_markup()


def creator_models_menu(provider: str, creator: str, settings=None, *, premium_models_allowed=None, grok_allowed=False):
    builder = InlineKeyboardBuilder()
    allowed = grok_allowed if premium_models_allowed is None else premium_models_allowed
    builder.button(text="🎲 Random Model", callback_data="random_model")
    for model in models_for_creator(provider, creator):
        label = ("🔒 " if requires_premium(model) and not allowed else "") + model.display_name
        builder.button(text=label, callback_data=f"model:{provider}:{creator}:{model.key}")
    builder.button(text="⬅️ Назад", callback_data=f"provider:{provider}")
    builder.adjust(1)
    return builder.as_markup()


def pages_keyboard(page: int, total: int):
    if total <= 1:
        return None
    builder = InlineKeyboardBuilder()
    if page > 0:
        builder.button(text="⬅️ Назад", callback_data=f"page:{page - 1}")
    builder.button(text=f"{page + 1}/{total}", callback_data="page_info")
    if page < total - 1:
        builder.button(text="Далее ➡️", callback_data=f"page:{page + 1}")
    builder.adjust(3)
    return builder.as_markup()


def all_callback_data(settings=None) -> tuple[str, ...]:
    values = ["models", "random_model", "new_chat", "support", "main_menu", "page_info", "acct:trusted", "acct:premium_models", "acct:status"]
    for key, _ in available_providers():
        values.append(f"provider:{key}")
        for creator in creators_for_backend(key):
            values.append(f"creator:{key}:{creator}")
            values.extend(
                f"model:{key}:{creator}:{model.key}"
                for model in models_for_creator(key, creator)
            )
    return tuple(values)
