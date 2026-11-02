"""Read-only contract inspection. Never print URLs, credentials or error bodies."""
import asyncio
import json
import os

import httpx
from dotenv import load_dotenv


async def main():
    load_dotenv()
    base = os.environ.get("AIS_API_BASE_URL", "").rstrip("/")
    key = os.environ.get("AIS_API_KEY", "")
    if not base or not key:
        print("AIS configuration missing")
        return
    root = base[:-3] if base.endswith("/v1") else base
    async with httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {key}"}) as client:
        for path in ("/v1/models", "/openapi.json"):
            try:
                response = await client.get(root + path)
                print("contract", path, "status", response.status_code)
                if response.status_code != 200:
                    continue
                value = response.json()
                if path == "/v1/models":
                    # This endpoint is public model metadata, never response bodies from inference.
                    print(json.dumps(value, ensure_ascii=True))
                else:
                    print("paths", json.dumps(value.get("paths", {}), ensure_ascii=True))
                    print("schemas", json.dumps(value.get("components", {}).get("schemas", {}), ensure_ascii=True))
            except Exception as error:
                print("contract failure", type(error).__name__)


if __name__ == "__main__":
    asyncio.run(main())
