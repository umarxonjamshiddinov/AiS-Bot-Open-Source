from __future__ import annotations

import asyncio
import re
from datetime import date
from urllib.parse import urlsplit

from tavily import AsyncTavilyClient


_EXPLICIT_SEARCH = re.compile(
    r"\b(найди|найти|поищи|поиск(?:ай|ать)?|проверь|проверить|"
    r"посмотри\s+в\s+интернете|search|find|look\s+up|verify|check\s+online)\b",
    re.IGNORECASE,
)
_CURRENT_INFO = re.compile(
    r"\b(сегодня|вчера|на\s+этой\s+неделе|последн(?:ий|ие|яя|ее)|"
    r"свеж(?:ий|ие|ая|ее)|актуальн(?:ый|ые|ая|ое)|текущ(?:ий|ие|ая|ее)|"
    r"новост(?:ь|и)|today|yesterday|latest|current|recent|news|this\s+week)\b",
    re.IGNORECASE,
)
_MODEL_QUERY = re.compile(
    r"\b(модел|deployment|развертыван|gpt|grok|kimi|deepseek|claude|gemini|llama|mistral)\w*\b",
    re.IGNORECASE,
)
_MODEL_DOMAINS = (
    "learn.microsoft.com",
    "openai.com",
    "x.ai",
    "deepseek.com",
    "moonshot.ai",
    "anthropic.com",
    "ai.google.dev",
    "mistral.ai",
)


def _official_model_url(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    return any(hostname == domain or hostname.endswith("." + domain) for domain in _MODEL_DOMAINS)


def should_search(text: str) -> bool:
    compact = " ".join(text.split())
    return bool(_EXPLICIT_SEARCH.search(compact) or _CURRENT_INFO.search(compact))


def is_model_query(text: str) -> bool:
    return bool(_MODEL_QUERY.search(" ".join(text.split())))


class WebSearchService:
    def __init__(self, api_key: str) -> None:
        self._client = AsyncTavilyClient(api_key=api_key)

    async def search(self, query: str) -> list[dict[str, str]]:
        model_query = is_model_query(query)
        if is_model_query(query):
            query = (
                f"{query[:600]} current as of {date.today().isoformat()} "
                "official model catalog release availability capabilities"
            )
        search_kwargs = {
            "query": query[:1000],
            "search_depth": "advanced" if model_query else "basic",
            "max_results": 8 if model_query else 5,
            "include_answer": False,
        }
        if model_query:
            search_kwargs["include_domains"] = list(_MODEL_DOMAINS)
        try:
            response = await asyncio.wait_for(
                self._client.search(**search_kwargs),
                timeout=20.0,
            )
        except Exception:
            return []
        results = [
            {
                "title": str(item.get("title", "")),
                "url": str(item.get("url", "")),
                "content": str(item.get("content", "")),
                "published_date": str(item.get("published_date", "")),
            }
            for item in response.get("results", [])
            if item.get("url")
        ]
        if model_query:
            results = [item for item in results if _official_model_url(item["url"])]
        return results

    @staticmethod
    def context(results: list[dict[str, str]]) -> str:
        if not results:
            return "Web search returned no results. Say that current sources were unavailable."
        parts = [
            "AiS performed a live web search. Use only these results for current claims "
            "and never invent additional URLs or citations. A missing result does not "
            "prove that a model, product, or release does not exist. Never use old "
            "training knowledge to contradict a model name supplied by AiS."
            " Current claims must be supported by a result whose content or date is "
            "relevant to the requested time. If no fresh source supports a claim, say "
            "that it could not be verified online instead of using knowledge from 2024."
            " Never call a model confirmed by independent sources unless those "
            "sources are actually present in the supplied results."
        ]
        for number, item in enumerate(results, start=1):
            parts.append(
                f"[{number}]\nTitle: {item['title']}\nURL: {item['url']}\n"
                f"Published: {item.get('published_date', '')}\nContent: {item['content']}"
            )
        return "\n\n".join(parts)

    @staticmethod
    def append_real_sources(answer: str, results: list[dict[str, str]]) -> str:
        allowed = {item["url"] for item in results}

        def keep_markdown_link(match: re.Match) -> str:
            return match.group(0) if match.group(2) in allowed else match.group(1)

        answer = re.sub(
            r"\[([^\]]+)\]\((https?://[^\s)]+)\)", keep_markdown_link, answer
        )

        def keep_plain_url(match: re.Match) -> str:
            token = match.group(0)
            url = token.rstrip(".,;:!?)]}")
            suffix = token[len(url):]
            return token if url in allowed else suffix

        answer = re.sub(r"https?://[^\s<]+", keep_plain_url, answer)
        missing = [item for item in results if item["url"] not in answer]
        if not missing:
            return answer
        lines = [answer.rstrip(), "", "Sources:"]
        lines.extend(f"- {item['title']}: {item['url']}" for item in missing)
        return "\n".join(lines)
