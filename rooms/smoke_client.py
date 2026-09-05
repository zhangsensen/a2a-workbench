from __future__ import annotations

import asyncio
import uuid

import httpx
from settings import BASE_URL

from a2a.client import ClientConfig, ClientFactory
from a2a.client.card_resolver import A2ACardResolver
from a2a.types import Message, Part, Role, SendMessageRequest


async def send_text(client, context_id: str, text: str) -> str:
    message = Message(
        role=Role.ROLE_USER,
        message_id=str(uuid.uuid4()),
        context_id=context_id,
        parts=[Part(text=text)],
    )
    events = []
    async for event in client.send_message(SendMessageRequest(message=message)):
        events.append(event)
    if not events or not events[-1].HasField("task"):
        raise RuntimeError("A2A client received no terminal task")
    task = events[-1].task
    response_parts = [
        part.text
        for artifact in task.artifacts
        for part in artifact.parts
        if part.HasField("text")
    ]
    if not response_parts:
        raise RuntimeError("A2A task contains no text artifact")
    return "\n".join(response_parts)


async def main() -> None:
    base_url = BASE_URL
    async with httpx.AsyncClient(timeout=650) as http_client:
        card = await A2ACardResolver(http_client, base_url).get_agent_card()
        client = ClientFactory(
            ClientConfig(httpx_client=http_client, streaming=False)
        ).create(card)
        context_id = f"smoke-{uuid.uuid4()}"
        created = await http_client.post(base_url + '/api/rooms', json={'id':context_id,'title':'Explicit A2A smoke test'})
        created.raise_for_status()
        marker = f"A2A_ROOM_{uuid.uuid4().hex[:8].upper()}"
        first = await send_text(
            client,
            context_id,
            f"请记住本房间临时暗号 {marker}，只回复：已记住。",
        )
        second = await send_text(
            client,
            context_id,
            "同一个 A2A 房间上一条消息的临时暗号是什么？只回复暗号。",
        )
        if marker not in second:
            raise RuntimeError(
                f"Persistent context check failed: expected {marker!r}, got {second!r}"
            )
        print(f"agent={card.name}")
        print(f"context_id={context_id}")
        print(f"first={first}")
        print(f"second={second}")
        print("persistent_context=ok")


if __name__ == "__main__":
    asyncio.run(main())
