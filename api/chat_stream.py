"""SSE transport: persistence is completed by the producer before terminal events."""

import asyncio
import json
import logging
import time
import uuid

import anyio
from langchain_core.callbacks import UsageMetadataCallbackHandler
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.responses import StreamingResponse

from api.chat_storage import ChatTurn
from src.agent.model_gateway import MODEL_ERROR_MESSAGES, classify_model_error
from src.agent.llm import active_model_profile

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: uuid.UUID
    request_id: uuid.UUID
    model_profile: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    message: str = Field(min_length=1, max_length=32000)

    @field_validator("message")
    @classmethod
    def nonempty_message(cls, value):
        if not value.strip():
            raise ValueError("消息不能为空")
        return value


def encode_event(name: str, payload: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def terminal_event(turn: ChatTurn):
    message = turn.assistant
    ids = {"session_id": str(turn.session_id), "request_id": str(turn.request_id),
           "message_id": str(turn.assistant_id), "replayed": turn.replayed}
    if message.status in {"completed", "stopped"}:
        name = "done" if message.status == "completed" else "stopped"
        answer = message.content + ("\n\n[本轮已停止生成]" if name == "stopped" else "")
        return name, {**ids, "answer": answer, "chat_log_id": str(turn.log_id),
                      "memory_status": message.memory_status or "unknown"}
    return "error", {**ids, "code": message.error_code,
                     "message": MODEL_ERROR_MESSAGES.get(
                         message.error_code,
                         "上次请求未完成，请使用新请求重试；异常中断的会话需要先核对上下文",
                     )}


def persisted_chat_response(turn, agent, chat_stream, usage_payload, model_profile=None):
    async def replay():
        name, payload = terminal_event(turn)
        yield encode_event(name, payload)

    async def stream():
        queue = asyncio.Queue()
        usage = UsageMetadataCallbackHandler()
        partial = ""
        last_saved = time.monotonic()
        started_at = last_saved
        first_token_at = None

        async def on_token(token):
            nonlocal partial, last_saved, first_token_at
            if first_token_at is None:
                first_token_at = time.monotonic()
            partial += token
            await queue.put(("token", {"content": token}))
            # One producer owns the DB session. Do not write on every token.
            if time.monotonic() - last_saved >= 1:
                await turn.save_partial(partial)
                last_saved = time.monotonic()

        async def on_activity(agent_name, status, details=None):
            if agent_name == "progress" and status == "started" and isinstance(details, dict):
                summary = details.get("summary")
                if isinstance(summary, str) and summary.strip():
                    event = await turn.append_event("answer", "started", summary.strip()[:240])
                    await queue.put(("agent_step", event))
                return
            if agent_name != "research" or status not in {"started", "completed", "failed"}:
                return
            summaries = {"started": "开始查找资料", "completed": "资料检索已完成", "failed": "资料检索失败"}
            if status == "failed" and isinstance(details, dict) and details.get("reason") == "limited":
                summaries["failed"] = "检索未完成，正在整理已获得的信息"
            if status == "completed" and isinstance(details, dict) and details.get("reason") == "no_results":
                summaries["completed"] = "未找到可核对的资料"
            sources = details.get("sources", []) if isinstance(details, dict) else []
            event = await turn.append_event("research", status, summaries[status], sources)
            await queue.put(("agent_step", event))

        async def record_failure(code, content, interrupted=False):
            event = await turn.append_event("answer", "stopped" if interrupted else "failed",
                                            "生成异常中断" if interrupted else "回答生成失败")
            await queue.put(("agent_step", event))
            await turn.finish("interrupted" if interrupted else "failed", content, code)
            logger.info(
                "chat_gateway request_id=%s status=%s code=%s duration_ms=%d first_token_ms=%s",
                turn.request_id, "interrupted" if interrupted else "failed", code,
                int((time.monotonic() - started_at) * 1000),
                int((first_token_at - started_at) * 1000) if first_token_at else "missing",
            )

        async def produce():
            profile_token = active_model_profile.set(model_profile)
            try:
                try:
                    result = await chat_stream(
                        message=turn.user_content, user_id=str(turn.owner_id),
                        session_id=str(turn.session_id), agent=agent,
                        on_token=on_token, callbacks=[usage], on_activity=on_activity,
                    )
                    status, answer = result[:2]
                    memory_status = result[2] if len(result) > 2 else "unknown"
                except asyncio.CancelledError:
                    # A model that propagates cancellation has not confirmed checkpoint cleanup.
                    with anyio.CancelScope(shield=True):
                        await record_failure("CHECKPOINT_RECOVERY_REQUIRED", partial, True)
                    await queue.put(("error", {
                        "code": "CHECKPOINT_RECOVERY_REQUIRED",
                        "message": "生成异常中断，请使用新会话或先核对上下文。",
                    }))
                    return
                except Exception as exc:
                    failure = classify_model_error(exc)
                    code, message = failure or (
                        "GENERATION_FAILED", "生成回复失败，请使用新的 request_id 重试。"
                    )
                    logger.exception("Chat generation failed; request_id=%s code=%s", turn.request_id, code)
                    await record_failure(code, partial)
                    await queue.put(("usage", usage_payload(usage)))
                    await queue.put(("error", {"code": code, "message": message}))
                    return

                if status not in {"done", "stopped"}:
                    raise ValueError("Unexpected chat terminal status")
                # Cancellation converted to 'stopped' by the agent must still be persisted.
                with anyio.CancelScope(shield=True):
                    answer_event = await turn.append_event(
                        "answer", "completed" if status == "done" else "stopped",
                        "回答已生成" if status == "done" else "回答已停止",
                    )
                    await queue.put(("agent_step", answer_event))
                    memory_event = await turn.append_event(
                        "memory", "failed" if memory_status == "failed" else
                        "skipped" if memory_status in {"skipped", "unknown"} else "completed",
                        {"saved": "记忆已保存", "unchanged": "记忆无变更", "failed": "记忆保存失败",
                         "skipped": "本轮无需保存记忆", "unknown": "记忆状态未知"}.get(memory_status, "记忆状态未知"),
                    )
                    await queue.put(("agent_step", memory_event))
                    await turn.finish("completed" if status == "done" else "stopped", answer or "",
                                      memory_status=memory_status)
                await queue.put(("usage", usage_payload(usage)))
                await queue.put(terminal_event(turn))
                logger.info(
                    "chat_gateway request_id=%s status=%s duration_ms=%d first_token_ms=%s",
                    turn.request_id, status, int((time.monotonic() - started_at) * 1000),
                    int((first_token_at - started_at) * 1000) if first_token_at else "missing",
                )
            except Exception:
                logger.exception("Saving chat result failed; request_id=%s", turn.request_id)
                with anyio.CancelScope(shield=True):
                    try:
                        await turn.db.rollback()
                        await record_failure("CHECKPOINT_RECOVERY_REQUIRED", partial, True)
                    except Exception:
                        logger.exception("Could not mark interrupted request_id=%s", turn.request_id)
                await queue.put(("error", {"code": "SAVE_FAILED", "message": "保存对话失败，执行结果待核对；请勿直接重复生成。"}))
            finally:
                active_model_profile.reset(profile_token)

        producer = asyncio.create_task(produce())
        try:
            yield ": connected\n\n"
            while True:
                name, payload = await queue.get()
                yield encode_event(name, payload)
                if name in {"done", "stopped", "error"}:
                    break
        finally:
            # Also runs when the client disconnects, before the DB and execution lock close.
            if not producer.done():
                producer.cancel()
            with anyio.CancelScope(shield=True):
                try:
                    await producer
                except asyncio.CancelledError:
                    pass

    return StreamingResponse(
        replay() if turn.replayed else stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
