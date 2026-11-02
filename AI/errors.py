"""Fixed user-facing messages, without raw SDK exception text."""
from AI.client import AISAPIError


def inference_error_message(error: Exception) -> str:
    if not isinstance(error, AISAPIError):
        return "❌ Не удалось получить ответ модели. Попробуйте позже."
    if error.kind == "timeout":
        return "⌛ Модель не успела ответить. Попробуйте позже."
    if error.kind == "connection":
        return "❌ Сервис AI временно недоступен. Попробуйте позже."
    if error.status_code in {400, 413, 422}:
        return "❌ Не удалось обработать запрос. Сократите сообщение или проверьте вложение."
    if error.status_code in {401, 403}:
        return "❌ Доступ к сервису AI временно недоступен. Обратитесь в поддержку."
    if error.status_code == 404:
        return "❌ Модель сейчас недоступна. Выберите другую модель."
    if error.status_code == 429:
        return "⏳ Сервис AI перегружен или достигнут лимит. Попробуйте позже."
    return "❌ Сервис AI временно недоступен. Попробуйте позже."
