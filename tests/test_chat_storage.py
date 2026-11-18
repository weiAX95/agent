"""Offline persistence tests using actual ORM and an isolated SQLite database."""

import unittest
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.base import Base
from api.models import AgentRun, ChatLog, ChatMessage, ChatSession, User
from api.chat_storage import begin_turn, create_session, owned_session
from api.run_storage import transition_run
from src.agent.run_policy import RunStatus


class ChatStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(self.engine.sync_engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.factory()
        self.owner = uuid.uuid4()
        self.other = uuid.uuid4()
        self.db.add_all([
            User(id=self.owner, username="alice", hashed_password="not-a-real-password"),
            User(id=self.other, username="bob", hashed_password="not-a-real-password"),
        ])
        await self.db.commit()
        self.session = await create_session(self.db, self.owner, "")

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def begin(self, request_id=None, message="你好"):
        return await begin_turn(self.db, self.owner, self.session.id, request_id or uuid.uuid4(), message)

    async def test_creation_has_authenticated_owner_defaults_and_thread(self):
        from src.agent.checkpoint import make_thread_id
        self.assertEqual(self.session.owner_id, self.owner)
        self.assertEqual(self.session.title, "新会话")
        self.assertEqual(self.session.next_message_seq, 1)
        self.assertEqual(self.session.checkpoint_thread_id, make_thread_id(str(self.owner), str(self.session.id)))

    async def test_owner_deleted_and_missing_sessions_are_hidden(self):
        for user, session_id in [(self.other, self.session.id), (self.owner, uuid.uuid4())]:
            with self.subTest(user=user):
                with self.assertRaises(HTTPException) as error:
                    await owned_session(self.db, user, session_id)
                self.assertEqual(error.exception.status_code, 404)
        self.session.deleted_at = datetime.now(UTC)
        await self.db.commit()
        with self.assertRaises(HTTPException):
            await self.begin()

    async def test_messages_committed_before_model_and_ordered_across_turns(self):
        turn = await self.begin()
        async with self.factory() as reader:
            rows = (await reader.scalars(select(ChatMessage).order_by(ChatMessage.seq))).all()
            self.assertEqual([(m.role, m.status, m.seq) for m in rows], [("user", "completed", 1), ("assistant", "streaming", 2)])
            run = await reader.get(AgentRun, turn.run_id)
            self.assertEqual((run.status, run.assistant_message_id), ("created", turn.assistant_id))
        await turn.start_run()
        await turn.finish("completed", "完整回复")
        self.assertEqual((await self.db.get(AgentRun, turn.run_id)).status, "succeeded")
        second = await self.begin(message="继续")
        self.assertEqual(second.assistant.seq, 4)
        self.assertEqual(self.session.next_message_seq, 5)

    async def test_completed_replay_does_not_add_messages_or_chatlog(self):
        request_id = uuid.uuid4()
        turn = await self.begin(request_id)
        await turn.start_run()
        log_id = await turn.finish("completed", "回复")
        replay = await self.begin(request_id)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.assistant.content, "回复")
        self.assertEqual(replay.log_id, log_id)
        self.assertEqual(len((await self.db.scalars(select(ChatMessage))).all()), 2)
        self.assertEqual(len((await self.db.scalars(select(ChatLog))).all()), 1)

    async def test_same_key_different_body_is_conflict(self):
        request_id = uuid.uuid4()
        turn = await self.begin(request_id)
        await turn.start_run()
        await turn.finish("completed", "回复")
        with self.assertRaises(HTTPException) as error:
            await self.begin(request_id, "不同问题")
        self.assertEqual(error.exception.status_code, 409)

    async def test_failed_turn_keeps_messages_without_success_log_and_replays_failure(self):
        request_id = uuid.uuid4()
        turn = await self.begin(request_id)
        await turn.start_run()
        await turn.finish("failed", "部分回复", "GENERATION_FAILED")
        replay = await self.begin(request_id)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.assistant.status, "failed")
        self.assertEqual(replay.assistant.content, "部分回复")
        self.assertEqual((await self.db.scalars(select(ChatLog))).all(), [])
        retry = await self.begin(message="显式重试")
        self.assertEqual(retry.assistant.seq, 4)

    async def test_stopped_reply_is_saved_without_marker_in_message(self):
        turn = await self.begin()
        await turn.start_run()
        log_id = await turn.finish("stopped", "部分回复")
        self.assertEqual(turn.assistant.content, "部分回复")
        self.assertEqual(turn.assistant.status, "stopped")
        self.assertIn("本轮已停止生成", (await self.db.get(ChatLog, log_id)).answer)

    async def test_orphan_stream_is_interrupted_and_blocks_blind_reexecution(self):
        turn = await self.begin()
        await turn.start_run()
        with self.assertRaises(HTTPException) as error:
            await self.begin(message="不能盲目重跑")
        self.assertEqual(error.exception.status_code, 409)
        await self.db.refresh(turn.assistant)
        self.assertEqual(turn.assistant.status, "interrupted")
        self.assertEqual(turn.assistant.error_code, "CHECKPOINT_RECOVERY_REQUIRED")
        # 未确认的执行保留 running，后续恢复流程才有权裁决终态。
        run = await self.db.get(AgentRun, turn.run_id)
        self.assertEqual((run.status, run.recovery_required), ("running", True))
        with self.assertRaises(HTTPException):
            await self.begin(message="仍然阻止")

    async def test_wrong_checkpoint_mapping_is_not_silently_used(self):
        self.session.checkpoint_thread_id = "legacy-thread"
        await self.db.commit()
        with self.assertRaises(HTTPException) as error:
            await self.begin()
        self.assertEqual(error.exception.status_code, 409)

    async def test_finish_failure_rolls_back_message_and_legacy_log_together(self):
        turn = await self.begin()
        await turn.start_run()
        commit = self.db.commit

        async def fail_commit():
            raise RuntimeError("simulated commit failure")

        self.db.commit = fail_commit
        with self.assertRaises(RuntimeError):
            await turn.finish("completed", "未提交的回复")
        self.db.commit = commit
        await self.db.rollback()
        rows = (await self.db.scalars(select(ChatMessage).where(ChatMessage.role == "assistant"))).all()
        self.assertEqual(rows[0].status, "streaming")
        self.assertEqual((await self.db.scalars(select(ChatLog))).all(), [])
        self.assertEqual((await self.db.get(AgentRun, turn.run_id)).status, "running")

    async def test_run_terminal_conflict_rolls_back_message(self):
        turn = await self.begin()
        await turn.start_run()
        self.assertTrue(await transition_run(
            self.db, turn.run_id, RunStatus.RUNNING, RunStatus.CANCELLED
        ))
        await self.db.commit()
        with self.assertRaisesRegex(RuntimeError, "Run 终态冲突"):
            await turn.finish("completed", "不能覆盖")
        async with self.factory() as reader:
            self.assertEqual((await reader.get(ChatMessage, turn.assistant_id)).status, "streaming")
            self.assertEqual((await reader.get(AgentRun, turn.run_id)).status, "cancelled")
            self.assertEqual((await reader.scalars(select(ChatLog))).all(), [])

    async def test_interrupted_run_requires_reconciliation(self):
        turn = await self.begin()
        await turn.start_run()
        await turn.finish("interrupted", "部分回复", "CHECKPOINT_RECOVERY_REQUIRED")
        run = await self.db.get(AgentRun, turn.run_id)
        self.assertEqual((run.status, run.recovery_required), ("running", True))
        self.assertFalse(await transition_run(
            self.db, turn.run_id, RunStatus.RUNNING, RunStatus.SUCCEEDED
        ))
        await self.db.rollback()

    async def test_request_cleanup_marks_run_for_recovery(self):
        turn = await self.begin()
        await turn.start_run()
        await turn.interrupt_if_streaming()
        run = await self.db.get(AgentRun, turn.run_id)
        self.assertEqual((run.status, run.recovery_required), ("running", True))
        self.assertEqual((await self.db.get(ChatMessage, turn.assistant_id)).status,
                         "interrupted")


if __name__ == "__main__":
    unittest.main()
