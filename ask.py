from __future__ import annotations

import argparse
import asyncio

import httpx
from settings import BASE_URL

from a2a.client import ClientConfig, ClientFactory
from a2a.client.card_resolver import A2ACardResolver

from smoke_client import send_text


async def ask(prompt: str, context_id: str) -> None:
    base_url = BASE_URL
    async with httpx.AsyncClient(timeout=650) as http_client:
        card = await A2ACardResolver(http_client, base_url).get_agent_card()
        client = ClientFactory(
            ClientConfig(httpx_client=http_client, streaming=False)
        ).create(card)
        print(await send_text(client, context_id, prompt))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Send one message to the persistent Codex/Claude/ZCode roundtable"
    )
    parser.add_argument("prompt")
    parser.add_argument("--context-id", required=True, help="Explicit existing room ID; list/create rooms before sending")
    args = parser.parse_args()
    asyncio.run(ask(args.prompt, args.context_id))
