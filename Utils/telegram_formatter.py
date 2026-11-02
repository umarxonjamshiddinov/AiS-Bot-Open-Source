import html
import re


def telegram_html(text: str) -> str:
    """Convert the bot's basic Markdown output to Telegram HTML."""
    code_blocks: list[str] = []

    def save_code_block(match: re.Match) -> str:
        language = match.group(1) or ""
        code = html.escape(match.group(2))
        index = len(code_blocks)
        if language:
            safe_language = html.escape(language)
            block = f'<pre><code class="language-{safe_language}">{code}</code></pre>'
        else:
            block = f"<pre>{code}</pre>"
        code_blocks.append(block)
        return f"@@CODEBLOCK{index}@@"

    text = re.sub(
        r"```([a-zA-Z0-9_+-]*)\n?(.*?)```",
        save_code_block,
        text,
        flags=re.DOTALL,
    )
    inline_codes: list[str] = []

    def save_inline_code(match: re.Match) -> str:
        index = len(inline_codes)
        inline_codes.append(f"<code>{html.escape(match.group(1))}</code>")
        return f"@@INLINECODE{index}@@"

    text = re.sub(r"`([^`\n]+)`", save_inline_code, text)
    text = html.escape(text)
    text = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", text)
    text = re.sub(r"(?m)^\s*---+\s*$", "────────────", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', text
    )
    text = re.sub(r"(?m)^\s*[-*]\s+", "• ", text)
    for index, code in enumerate(inline_codes):
        text = text.replace(f"@@INLINECODE{index}@@", code)
    for index, block in enumerate(code_blocks):
        text = text.replace(f"@@CODEBLOCK{index}@@", block)
    return text


def split_pages(text: str, limit: int = 3500) -> list[str]:
    """Split long Markdown into Telegram-safe pages without losing content."""
    if len(text) <= limit:
        return [text]
    pages: list[str] = []
    remaining = text
    while len(remaining) > limit:
        candidates = (
            remaining.rfind("\n\n", 0, limit),
            remaining.rfind("\n", 0, limit),
            remaining.rfind(" ", 0, limit),
        )
        split_at = next((value for value in candidates if value > 0), limit)
        page = remaining[:split_at].strip()
        if page:
            pages.append(page)
        remaining = remaining[split_at:].strip()
    if remaining:
        pages.append(remaining)
    return pages
