"""真实 PostgreSQL Run 迁移与并发测试；只在一次性测试数据库中运行。"""

import asyncio
import json
import os
import sys
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from fastapi import HTTPException
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from api.chat_storage import ChatTurn, begin_turn
from api.run_recovery import abandon_run_after_review, inspect_run_recovery
from api.models import AgentRun, AgentRunReconciliation, ChatLog, ChatMessage, ChatSession, User
from api.run_storage import mark_run_recovery_required, new_run, record_run_step, transition_run
from src.agent.checkpoint import checkpoint_dsn, make_thread_id
from src.agent.run_policy import RunLimits, RunStatus


DB_URL = os.getenv("RUN_TEST_POSTGRES_URL")


@unittest.skipUnless(
    DB_URL and os.getenv("RUN_TEST_POSTGRES_ALLOW_REBUILD") == "1",
    "设置 RUN_TEST_POSTGRES_URL 与 RUN_TEST_POSTGRES_ALLOW_REBUILD=1 指向一次性测试库",
)
class RunPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # 测试会重建表；必须使用名称明确的隔离测试库，绝不碰业务数据库。
        if make_url(DB_URL).database != "gateway_m2_test":
            raise ValueError("Run PostgreSQL 测试仅允许 gateway_m2_test 数据库")
        self.engine = create_async_engine(DB_URL)
        async with self.engine.begin() as connection:
            await connection.execute(text("DROP TABLE IF EXISTS public.agent_run_reconciliations"))
            await connection.execute(text("DROP TABLE IF EXISTS public.agent_runs"))
            await connection.execute(text("DROP TABLE IF EXISTS public.side_effect_log"))
            for table in (ChatLog.__table__, ChatMessage.__table__, ChatSession.__table__, User.__table__):
                await connection.run_sync(table.drop, checkfirst=True)
            for table in (User.__table__, ChatSession.__table__, ChatMessage.__table__, ChatLog.__table__):
                await connection.run_sync(table.create)
            # 模拟目标库已有的旧版会话与消息，再执行新增 Run 表的前向迁移。
            self.legacy_owner, self.legacy_session, self.legacy_message = (
                uuid.uuid4() for _ in range(3)
            )
            await connection.execute(User.__table__.insert().values(
                id=self.legacy_owner, username=f"legacy-{self.legacy_owner}",
                hashed_password="test",
            ))
            await connection.execute(ChatSession.__table__.insert().values(
                id=self.legacy_session, owner_id=self.legacy_owner,
                title="迁移前会话", checkpoint_thread_id=str(self.legacy_session),
                next_message_seq=2,
            ))
            await connection.execute(ChatMessage.__table__.insert().values(
                id=self.legacy_message, session_id=self.legacy_session,
                request_id=uuid.uuid4(), seq=1, role="assistant",
                status="completed", content="迁移前答案", finished_at=datetime.now(UTC),
            ))
            migration = Path("migrations/20261117_agent_runs.sql").read_text()
            # asyncpg 预编译语句只接受单条 SQL，按迁移文件中的语句顺序逐条执行。
            statements = [part.strip() for part in migration.split(";")
                          if "CREATE TABLE" in part or "ALTER TABLE" in part]
            for _ in range(2):
                for statement in statements:
                    await connection.exec_driver_sql(statement)
            self.assertIsNotNone(await connection.scalar(text("SELECT to_regclass('public.agent_runs')")))
            self.assertIsNotNone(await connection.scalar(text(
                "SELECT to_regclass('public.agent_run_reconciliations')"
            )))
            self.assertEqual(await connection.scalar(
                select(ChatMessage.content).where(ChatMessage.id == self.legacy_message)
            ), "迁移前答案")
            await connection.execute(text(
                "CREATE TABLE public.side_effect_log (id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY)"
            ))
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.owner, self.session_id, self.message_id, self.request_id = (
            uuid.uuid4() for _ in range(4)
        )
        async with self.sessions() as db:
            db.add(User(id=self.owner, username=f"run-{self.owner}", hashed_password="test"))
            await db.flush()
            db.add(ChatSession(id=self.session_id, owner_id=self.owner,
                               title="测试", checkpoint_thread_id=str(self.session_id)))
            await db.flush()
            db.add(ChatMessage(id=self.message_id, session_id=self.session_id,
                               request_id=self.request_id, seq=1, role="assistant",
                               status="streaming", content=""))
            # ORM 依赖次序由 flush 显式保证，避免测试与生产写入顺序不同。
            await db.flush()
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_legacy_chat_data_survives_forward_and_schema_rollback(self):
        async with self.engine.begin() as connection:
            self.assertEqual(await connection.scalar(
                select(ChatSession.next_message_seq).where(ChatSession.id == self.legacy_session)
            ), 2)
            await connection.execute(text("DROP TABLE public.agent_run_reconciliations"))
            await connection.execute(text("DROP TABLE public.agent_runs"))
            self.assertEqual(await connection.scalar(
                select(ChatMessage.content).where(ChatMessage.id == self.legacy_message)
            ), "迁移前答案")
            self.assertEqual(await connection.scalar(
                select(ChatMessage.status).where(ChatMessage.id == self.legacy_message)
            ), "completed")

    async def test_migration_and_terminal_cas_under_concurrency(self):
        run = new_run(self.owner, self.session_id, self.request_id, self.message_id,
                      RunLimits(max_steps=2, timeout_seconds=10))
        async with self.sessions() as db:
            db.add(run)
            await db.commit()
            self.assertTrue(await transition_run(db, run.id, RunStatus.CREATED, RunStatus.RUNNING))
            await db.commit()

        async def claim_first_step():
            async with self.sessions() as db:
                won = await record_run_step(db, run.id, 1)
                await db.commit()
                return won

        step_outcomes = await asyncio.gather(claim_first_step(), claim_first_step())
        self.assertEqual(sorted(step_outcomes), [False, True])

        async def finish(target):
            async with self.sessions() as db:
                won = await transition_run(db, run.id, RunStatus.RUNNING, target)
                await db.commit()
                return won

        outcomes = await asyncio.gather(finish(RunStatus.SUCCEEDED),
                                        finish(RunStatus.CANCELLED))
        self.assertEqual(sorted(outcomes), [False, True])
        async with self.sessions() as db:
            stored = await db.scalar(select(AgentRun).where(AgentRun.id == run.id))
            self.assertIn(stored.status, {RunStatus.SUCCEEDED, RunStatus.CANCELLED})
            self.assertEqual(stored.steps, 1)
            self.assertIsNotNone(stored.finished_at)
            self.assertGreater(stored.deadline_at.replace(tzinfo=UTC), datetime.now(UTC))

    async def test_chat_finish_and_cancel_commit_one_consistent_terminal(self):
        run = new_run(self.owner, self.session_id, self.request_id, self.message_id,
                      RunLimits(max_steps=3, timeout_seconds=30))
        async with self.sessions() as db:
            db.add(run)
            await db.commit()
            self.assertTrue(await transition_run(db, run.id, RunStatus.CREATED, RunStatus.RUNNING))
            await db.commit()

        async def finish(status, answer):
            async with self.sessions() as db:
                session = await db.get(ChatSession, self.session_id)
                assistant = await db.get(ChatMessage, self.message_id)
                turn = ChatTurn(db, session, assistant, "同一请求", run_id=run.id)
                try:
                    await turn.finish(status, answer)
                    return status
                except RuntimeError as exc:
                    self.assertIn("其他 Worker", str(exc))
                    return "lost"

        # 两个独立连接同时提交，消息和 Run 必须由同一个获胜事务裁决。
        outcomes = await asyncio.gather(
            finish("completed", "正常答案"), finish("stopped", "取消答案")
        )
        self.assertEqual(outcomes.count("lost"), 1)
        winner = next(value for value in outcomes if value != "lost")
        async with self.sessions() as db:
            message = await db.get(ChatMessage, self.message_id)
            stored = await db.get(AgentRun, run.id)
            logs = (await db.scalars(select(ChatLog).where(ChatLog.id == self.message_id))).all()
            self.assertEqual(message.status, winner)
            self.assertEqual(stored.status, {
                "completed": RunStatus.SUCCEEDED, "stopped": RunStatus.CANCELLED,
            }[winner])
            self.assertEqual(len(logs), 1)

    async def test_worker_loss_keeps_run_and_request_unique(self):
        run = new_run(self.owner, self.session_id, self.request_id, self.message_id,
                      RunLimits(max_steps=2, timeout_seconds=10))
        async with self.sessions() as db:
            db.add(run)
            await db.commit()
            self.assertTrue(await transition_run(db, run.id, RunStatus.CREATED, RunStatus.RUNNING))
            await db.commit()
        # 新连接模拟 Worker 重启：不重放模型，只读取尚未裁决的执行事实。
        async with self.sessions() as recovered:
            stored = await recovered.get(AgentRun, run.id)
            self.assertEqual(stored.status, RunStatus.RUNNING)
            self.assertEqual(stored.request_id, self.request_id)
            self.assertTrue(await mark_run_recovery_required(recovered, run.id))
            await recovered.commit()
            self.assertFalse(await record_run_step(recovered, run.id, 1))
            self.assertFalse(await transition_run(
                recovered, run.id, RunStatus.RUNNING, RunStatus.SUCCEEDED
            ))
            await recovered.rollback()
            recovered.add(new_run(self.owner, self.session_id, self.request_id,
                                  self.message_id, RunLimits()))
            with self.assertRaises(IntegrityError):
                await recovered.commit()
            await recovered.rollback()

    async def test_killed_worker_cannot_replay_side_effect(self):
        crash_session = uuid.uuid4()
        async with self.sessions() as db:
            db.add(ChatSession(
                id=crash_session, owner_id=self.owner, title="故障注入",
                checkpoint_thread_id=make_thread_id(str(self.owner), str(crash_session)),
            ))
            await db.commit()

        child_code = """
import asyncio, os, uuid
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from api.chat_storage import begin_turn

async def main():
    engine = create_async_engine(os.environ['RUN_TEST_POSTGRES_URL'])
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        turn = await begin_turn(db, uuid.UUID(os.environ['RUN_TEST_OWNER']),
                                uuid.UUID(os.environ['RUN_TEST_SESSION']),
                                uuid.uuid4(), '故障注入')
        await turn.start_run()
        await db.execute(text('INSERT INTO public.side_effect_log DEFAULT VALUES'))
        await db.commit()
        os._exit(23)

asyncio.run(main())
"""
        env = dict(os.environ, RUN_TEST_OWNER=str(self.owner),
                   RUN_TEST_SESSION=str(crash_session))
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-c", child_code, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await child.communicate()
        self.assertEqual(child.returncode, 23, err.decode())

        async with self.sessions() as recovered:
            with self.assertRaises(HTTPException) as blocked:
                await begin_turn(recovered, self.owner, crash_session,
                                 uuid.uuid4(), "不应重放")
            self.assertEqual(blocked.exception.status_code, 409)
            run = await recovered.scalar(select(AgentRun).where(
                AgentRun.session_id == crash_session
            ))
            self.assertEqual((run.status, run.recovery_required), ("running", True))
            side_effect_count = await recovered.scalar(text(
                "SELECT count(*) FROM public.side_effect_log"
            ))
            self.assertEqual(side_effect_count, 1)

    async def test_checkpoint_survives_worker_crash_without_replaying_tool(self):
        crash_session = uuid.uuid4()
        thread_id = make_thread_id(str(self.owner), str(crash_session))
        async with self.sessions() as db:
            db.add(ChatSession(id=crash_session, owner_id=self.owner,
                               title="checkpoint 故障", checkpoint_thread_id=thread_id))
            await db.commit()

        child_code = """
import asyncio, os, uuid
from typing import TypedDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from api.chat_storage import begin_turn
from src.agent.checkpoint import checkpoint_dsn

class State(TypedDict):
    marker: str

async def main():
    url = os.environ['RUN_TEST_POSTGRES_URL']
    engine = create_async_engine(url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        turn = await begin_turn(db, uuid.UUID(os.environ['RUN_TEST_OWNER']),
                                uuid.UUID(os.environ['RUN_TEST_SESSION']),
                                uuid.uuid4(), 'checkpoint 故障')
        await turn.start_run()
    async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn(url)) as saver:
        await saver.setup()
        graph = StateGraph(State)
        async def side_effect(_state):
            async with engine.begin() as conn:
                await conn.execute(text('INSERT INTO public.side_effect_log DEFAULT VALUES'))
            return {'marker': 'tool-finished'}
        graph.add_node('tool', side_effect)
        graph.add_edge(START, 'tool')
        await graph.compile(checkpointer=saver).ainvoke(
            {'marker': 'started'},
            config={'configurable': {'thread_id': os.environ['RUN_TEST_THREAD']}},
        )
    await engine.dispose()
    # 模拟 checkpoint 已落盘、消息与 Run 终态尚未提交时 Worker 被杀死。
    os._exit(23)

asyncio.run(main())
"""
        env = dict(os.environ, RUN_TEST_OWNER=str(self.owner),
                   RUN_TEST_SESSION=str(crash_session), RUN_TEST_THREAD=thread_id)
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-c", child_code, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await child.communicate()
        self.assertEqual(child.returncode, 23, err.decode())

        async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn(DB_URL)) as saver:
            checkpoint = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint.checkpoint["channel_values"]["marker"], "tool-finished")

        async with self.sessions() as recovered:
            with self.assertRaises(HTTPException) as blocked:
                await begin_turn(recovered, self.owner, crash_session,
                                 uuid.uuid4(), "不能重放工具")
            self.assertEqual(blocked.exception.status_code, 409)
            run = await recovered.scalar(select(AgentRun).where(AgentRun.session_id == crash_session))
            run_id = run.id
            self.assertTrue(run.recovery_required)
            async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn(DB_URL)) as saver:
                snapshot = await inspect_run_recovery(recovered, saver, self.owner, run_id)
            self.assertEqual(snapshot["run_status"], "running")
            self.assertEqual(snapshot["message_status"], "interrupted")
            self.assertEqual(snapshot["checkpoint_id"], checkpoint.checkpoint["id"])
            self.assertFalse(snapshot["external_effects_verified"])
            inspection = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "tools.inspect_run",
                "--run-id", str(run_id), "--owner-id", str(self.owner),
                env=os.environ.copy(), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            inspection_out, inspection_err = await inspection.communicate()
            self.assertEqual(inspection.returncode, 0, inspection_err.decode())
            self.assertEqual(json.loads(inspection_out)["checkpoint_id"],
                             checkpoint.checkpoint["id"])
            self.assertNotIn(b"tool-finished", inspection_out)
            self.assertEqual(await recovered.scalar(text(
                "SELECT count(*) FROM public.side_effect_log"
            )), 1)
            async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn(DB_URL)) as saver:
                with self.assertRaises(HTTPException) as changed:
                    await abandon_run_after_review(
                        recovered, saver, self.owner, run_id, self.owner, "wrong",
                        "confirmed", "ticket:INC-123",
                    )
                self.assertEqual(changed.exception.status_code, 409)
            command = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "tools.abandon_run",
                "--run-id", str(run_id), "--owner-id", str(self.owner),
                "--reviewer-id", str(self.owner),
                "--checkpoint-id", checkpoint.checkpoint["id"],
                "--external-effect-status", "confirmed",
                "--evidence-ref", "ticket:INC-123", "--confirm-abandon",
                env=os.environ.copy(), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await command.communicate()
            self.assertEqual(command.returncode, 0, err.decode())
            outcome = json.loads(out)
            self.assertEqual(outcome, {"run_id": str(run_id), "status": RunStatus.FAILED,
                                       "session_reusable": False})
            await recovered.refresh(run)
            self.assertEqual((run.status, run.recovery_required), (RunStatus.FAILED, False))
            audit = await recovered.get(AgentRunReconciliation, run_id)
            self.assertEqual((audit.reviewer_id, audit.evidence_ref),
                             (self.owner, "ticket:INC-123"))
            async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn(DB_URL)) as saver:
                with self.assertRaises(HTTPException) as repeated:
                    await abandon_run_after_review(
                        recovered, saver, self.owner, run_id, self.owner,
                        checkpoint.checkpoint["id"], "confirmed", "ticket:INC-123",
                    )
            self.assertEqual(repeated.exception.status_code, 409)
            with self.assertRaises(HTTPException) as still_blocked:
                await begin_turn(recovered, self.owner, crash_session,
                                 uuid.uuid4(), "不能恢复旧会话")
            self.assertEqual(still_blocked.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
