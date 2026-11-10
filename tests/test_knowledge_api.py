"""Management policy and durable request idempotency using real async SQL sessions."""
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from fastapi import HTTPException
from api.base import Base
from api.models import User, KnowledgeDocument
from api.knowledge_models import KnowledgeIndexJob, KnowledgeChunkRecord, KnowledgeAuditEvent
from api.knowledge_policy import require_knowledge_manager
from src.agent.knowledge_ingest import prepare_document


class KnowledgePolicyTests(unittest.TestCase):
    def test_shared_default_and_admin_allowlist_do_not_trust_client_role(self):
        actor = uuid.uuid4()
        with patch.dict(os.environ, {'KNOWLEDGE_MANAGEMENT_MODE':'shared'}):
            self.assertEqual(require_knowledge_manager({'user_id':str(actor)}), actor)
        with patch.dict(os.environ, {'KNOWLEDGE_MANAGEMENT_MODE':'admin','KNOWLEDGE_ADMIN_IDS':str(uuid.uuid4())}):
            with self.assertRaises(HTTPException) as error:
                require_knowledge_manager({'user_id':str(actor), 'role':'admin'})
            self.assertEqual(error.exception.status_code, 403)
        with patch.dict(os.environ, {'KNOWLEDGE_MANAGEMENT_MODE':'admin','KNOWLEDGE_ADMIN_IDS':str(actor)}):
            self.assertEqual(require_knowledge_manager({'user_id':str(actor)}), actor)


class KnowledgeApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'KNOWLEDGE_ARCHIVE_DIR':self.tmp.name,
            'DATABASE_URL':'sqlite+aiosqlite://', 'JWT_SIGNING_KEY':'offline-test-only',
            'JWT_VERIFYING_KEY':'offline-test-only', 'JWT_ISSUER':'offline', 'JWT_AUDIENCE':'offline'})
        self.env.start(); self.addCleanup(self.env.stop)
        from api.routers import knowledge
        self.api = knowledge
        self.engine = create_async_engine('sqlite+aiosqlite://')
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda c: Base.metadata.create_all(c, tables=[User.__table__,
                KnowledgeDocument.__table__, KnowledgeIndexJob.__table__, KnowledgeChunkRecord.__table__, KnowledgeAuditEvent.__table__]))
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.actor = uuid.uuid4()
        async with self.sessions() as session:
            session.add(User(id=self.actor, username='offline', hashed_password='not-a-login'))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_duplicate_request_has_one_durable_job_and_conflict_does_not_overwrite(self):
        request = uuid.uuid4()
        prepared = prepare_document('sample.md', '合成原文。'.encode())
        async with self.sessions() as session:
            first = await self.api._submit(session, prepared, self.actor, request_id=request, background=True)
            second = await self.api._submit(session, prepared, self.actor, request_id=request, background=True)
            self.assertEqual((first.status_code, second.status_code), (202,202))
            self.assertEqual(await session.scalar(select(func.count()).select_from(KnowledgeIndexJob)), 1)
            with self.assertRaises(HTTPException) as conflict:
                await self.api._submit(session, prepare_document('sample.md', b'changed'), self.actor,
                                       request_id=request, background=True)
            self.assertEqual(conflict.exception.status_code, 409)
            current = await session.get(KnowledgeIndexJob, request)
            self.assertEqual(current.sha256, prepared.sha256)

    async def test_only_failed_jobs_can_retry_and_action_is_audited(self):
        from src.agent.knowledge_jobs import make_job
        job = make_job(prepared=prepare_document('sample.md', b'content'), actor_id=self.actor)
        job.status = 'failed'; job.attempts = 1
        async with self.sessions() as session:
            session.add(job); await session.commit()
            result = await self.api.retry_index_job(job.id, {'user_id':str(self.actor)}, session)
            self.assertEqual(result['data']['status'], 'queued')
            self.assertIsNotNone(await session.scalar(select(KnowledgeAuditEvent).where(KnowledgeAuditEvent.action == 'index_retry')))
            with self.assertRaises(HTTPException):
                await self.api.retry_index_job(job.id, {'user_id':str(self.actor)}, session)

    async def test_unique_conflict_rechecks_filename_after_stale_initial_read(self):
        request=uuid.uuid4()
        original=prepare_document('first.md', b'same content')
        async with self.sessions() as session:
            await self.api._submit(session,original,self.actor,request_id=request,background=True)
        async with self.sessions() as session:
            real_get=session.get
            first=True
            async def stale_get(*args,**kwargs):
                nonlocal first
                if first:
                    first=False
                    return None
                return await real_get(*args,**kwargs)
            with patch.object(session,'get',side_effect=stale_get):
                with self.assertRaises(HTTPException) as conflict:
                    await self.api._submit(session,prepare_document('different.md', b'same content'),
                        self.actor,request_id=request,background=True)
            self.assertEqual(conflict.exception.status_code,409)
