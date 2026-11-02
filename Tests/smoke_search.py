"""Explicit Tavily smoke test. Never run automatically."""

import asyncio

from Search.web_search import WebSearchService
from config import Settings


async def run() -> None:
    service = WebSearchService(Settings.load().tavily_api_key)
    for item in await service.search("Последние новости AWS Bedrock"):
        print(item["title"], item["url"])


if __name__ == "__main__":
    asyncio.run(run())
