"""A2A request handler that makes requestId retries idempotent."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.request_handlers.request_handler import validate_request_params
from google.protobuf.json_format import MessageToDict

from common.idempotency import RequestDedup


class DedupRequestHandler(DefaultRequestHandler):
    """Return the original persisted task when an A2A request is retried."""

    def __init__(self, *args: Any, request_dedup: RequestDedup, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.request_dedup = request_dedup

    @staticmethod
    def _request_id(params: Any) -> object | None:
        metadata = getattr(params, "metadata", None)
        if isinstance(metadata, dict):
            return metadata.get("requestId")
        if metadata is None:
            return None
        try:
            return MessageToDict(metadata).get("requestId")
        except (AttributeError, TypeError, ValueError):
            return None

    async def _existing_task(self, params: Any, context: Any) -> Any | None:
        request_id = self._request_id(params)
        task_id = self.request_dedup.lookup(request_id)
        if not task_id:
            return None
        return await self.task_store.get(task_id, context)

    async def _setup_active_task(self, params: Any, context: Any) -> Any:
        """Remember the generated ID before waiting for the agent's final result."""
        active_task, request_context = await super()._setup_active_task(params, context)
        request_id = self._request_id(params)
        if request_id and request_context.task_id:
            self.request_dedup.remember(request_id, request_context.task_id)
        return active_task, request_context

    @validate_request_params
    async def on_message_send(self, params: Any, context: Any) -> Any:
        existing = await self._existing_task(params, context)
        if existing is not None:
            return existing

        result = await super().on_message_send(params, context)
        request_id = self._request_id(params)
        task_id = getattr(result, "id", None)
        if request_id and task_id:
            self.request_dedup.remember(request_id, task_id)
        return result

    @validate_request_params
    async def on_message_send_stream(
        self, params: Any, context: Any
    ) -> AsyncGenerator[Any, None]:
        existing = await self._existing_task(params, context)
        if existing is not None:
            yield existing
            return

        async for event in super().on_message_send_stream(params, context):
            yield event
