"""在 HTTP 传输边界识别每次真实发送，供模型重试逐次入账。"""

import asyncio
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

import httpx


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

    def _finish(self, status: str) -> None:
        if not self.done:
            self.sink.finish(self.call_id, status)
            self.done = True

    def __iter__(self):
        try:
            yield from self.source
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

    async def _finish(self, status: str) -> None:
        if not self.done:
            await self.sink.afinish(self.call_id, status)
            self.done = True

    async def __aiter__(self):
        try:
            async for chunk in self.source:
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
            self.sink.finish(call_id, "succeeded" if response.status_code < 400 else "failed")
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
            await self.sink.afinish(call_id, "succeeded" if response.status_code < 400 else "failed")
        else:
            response.stream = _AsyncStream(response.stream, self.sink, call_id,
                                           response.status_code < 400)
        return response
