"""在 HTTP 传输边界识别每次真实发送，供模型重试逐次入账。"""

import asyncio
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

import httpx

from src.agent.provider_usage import ProviderUsageParser
from src.agent.usage_pricing import TokenUsage


@dataclass(frozen=True)
class MeteredRun:
    run_id: uuid.UUID
    owner_id: uuid.UUID
    trace_id: str | None = None


active_metered_run: ContextVar[MeteredRun | None] = ContextVar(
    "active_metered_run", default=None,
)


class AttemptSink(Protocol):
    def start(self, run: MeteredRun, provider: str, model: str) -> uuid.UUID: ...
    def finish(self, call_id: uuid.UUID, status: str) -> None: ...
    async def astart(self, run: MeteredRun, provider: str, model: str) -> uuid.UUID: ...
    async def afinish(self, call_id: uuid.UUID, status: str) -> None: ...
    def record_usage(self, call_id: uuid.UUID, status: str, usage: TokenUsage) -> None: ...
    async def arecord_usage(self, call_id: uuid.UUID, status: str, usage: TokenUsage) -> None: ...


def _is_model_request(request: httpx.Request) -> bool:
    path = request.url.path.rstrip("/")
    return request.method == "POST" and (
        path.endswith("/chat/completions") or path.endswith("/responses")
    )


def _error_status(exc: BaseException) -> str:
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "timed_out"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    return "unknown"


class _SyncStream(httpx.SyncByteStream):
    def __init__(self, source: httpx.SyncByteStream, sink: AttemptSink,
                 call_id: uuid.UUID, success: bool):
        self.source, self.sink, self.call_id = source, sink, call_id
        self.success, self.done = success, False
        self.parser = ProviderUsageParser()

    def _finish(self, status: str) -> None:
        if not self.done:
            self.sink.finish(self.call_id, status)
            self.done = True
            usage = self.parser.finish()
            if usage is not None:
                self.sink.record_usage(self.call_id, status, usage)

    def __iter__(self):
        try:
            for chunk in self.source:
                self.parser.feed(chunk)
                yield chunk
        except BaseException as exc:
            self._finish(_error_status(exc))
            raise
        else:
            self._finish("succeeded" if self.success else "failed")

    def close(self) -> None:
        try:
            self.source.close()
        finally:
            self._finish("cancelled")


class _AsyncStream(httpx.AsyncByteStream):
    def __init__(self, source: httpx.AsyncByteStream, sink: AttemptSink,
                 call_id: uuid.UUID, success: bool):
        self.source, self.sink, self.call_id = source, sink, call_id
        self.success, self.done = success, False
        self.parser = ProviderUsageParser()

    async def _finish(self, status: str) -> None:
        if not self.done:
            await self.sink.afinish(self.call_id, status)
            self.done = True
            usage = self.parser.finish()
            if usage is not None:
                await self.sink.arecord_usage(self.call_id, status, usage)

    async def __aiter__(self):
        try:
            async for chunk in self.source:
                self.parser.feed(chunk)
                yield chunk
        except BaseException as exc:
            await self._finish(_error_status(exc))
            raise
        else:
            await self._finish("succeeded" if self.success else "failed")

    async def aclose(self) -> None:
        try:
            await self.source.aclose()
        finally:
            await self._finish("cancelled")


class MeteredClient(httpx.Client):
    def __init__(self, sink: AttemptSink, provider: str, model: str, **kwargs):
        super().__init__(**kwargs)
        self.sink, self.provider, self.model = sink, provider, model

    def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        run = active_metered_run.get()
        if run is None or not _is_model_request(request):
            return super().send(request, **kwargs)
        # start 事务必须先提交；异常时直接拒绝发送，避免产生无主上游费用。
        call_id = self.sink.start(run, self.provider, self.model)
        try:
            response = super().send(request, **kwargs)
        except BaseException as exc:
            self.sink.finish(call_id, _error_status(exc))
            raise
        if response.is_closed:
            status = "succeeded" if response.status_code < 400 else "failed"
            self.sink.finish(call_id, status)
            parser = ProviderUsageParser()
            parser.feed(response.content)
            usage = parser.finish()
            if usage is not None:
                self.sink.record_usage(call_id, status, usage)
        else:
            response.stream = _SyncStream(response.stream, self.sink, call_id,
                                          response.status_code < 400)
        return response


class AsyncMeteredClient(httpx.AsyncClient):
    def __init__(self, sink: AttemptSink, provider: str, model: str, **kwargs):
        super().__init__(**kwargs)
        self.sink, self.provider, self.model = sink, provider, model

    async def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        run = active_metered_run.get()
        if run is None or not _is_model_request(request):
            return await super().send(request, **kwargs)
        call_id = await self.sink.astart(run, self.provider, self.model)
        try:
            response = await super().send(request, **kwargs)
        except BaseException as exc:
            await self.sink.afinish(call_id, _error_status(exc))
            raise
        if response.is_closed:
            status = "succeeded" if response.status_code < 400 else "failed"
            await self.sink.afinish(call_id, status)
            parser = ProviderUsageParser()
            parser.feed(response.content)
            usage = parser.finish()
            if usage is not None:
                await self.sink.arecord_usage(call_id, status, usage)
        else:
            response.stream = _AsyncStream(response.stream, self.sink, call_id,
                                           response.status_code < 400)
        return response
