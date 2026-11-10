"""Exercise real SQL transactions with SQLite, and fake only vector writes."""
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from api.base import Base
from api.models import KnowledgeDocument, User
from api.knowledge_models import KnowledgeChunkRecord, KnowledgeAuditEvent, KnowledgeIndexJob
from src.agent.knowledge_jobs import make_job, run_job, retry_job, claim, reconcile
from src.agent.knowledge_ingest import prepare_document


class KnowledgeJobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'KNOWLEDGE_ARCHIVE_DIR':self.tmp.name})
        self.env.start(); self.addCleanup(self.env.stop)
        self.engine = create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine, tables=[User.__table__, KnowledgeDocument.__table__,
            KnowledgeChunkRecord.__table__, KnowledgeIndexJob.__table__, KnowledgeAuditEvent.__table__])
        self.actor = uuid.uuid4()
        self.vectors = {}
        self.cleaned = []

    def submit(self, text='必须停机。', **kwargs):
        prepared = prepare_document('example.md', text.encode())
        job = make_job(prepared=prepared, actor_id=self.actor, **kwargs)
        with Session(self.engine) as session, session.begin():
            session.add(job); session.flush(); session.expunge(job)
        return job

    def index(self, doc, revision, filename, chunks):
        self.vectors[revision] = [c.text for c in chunks]

    def clean(self, doc, revision):
        self.cleaned.append(revision)
        self.vectors.pop(revision, None)

    def run_job(self, job, indexer=None):
        return run_job(self.engine, job.id, indexer=indexer or self.index, cleaner=self.clean, heartbeat=False)

    def test_publish_is_atomic_and_finished_job_is_not_repeated(self):
        job = self.submit()
        self.assertEqual(self.run_job(job), 'succeeded')
        self.assertIsNone(self.run_job(job))
        with Session(self.engine) as session:
            self.assertEqual(session.get(KnowledgeDocument, job.document_id).revision, job.revision)
            self.assertEqual(session.scalar(select(KnowledgeChunkRecord)).text, '必须停机。')
            self.assertEqual(session.get(KnowledgeIndexJob, job.id).status, 'succeeded')

    def test_failed_write_cannot_publish_and_retry_is_idempotent(self):
        job = self.submit()
        def partial(*args): self.index(*args); raise RuntimeError('partial vector write')
        self.assertEqual(self.run_job(job, partial), 'failed')
        with Session(self.engine) as session, session.begin():
            self.assertIsNone(session.get(KnowledgeDocument, job.document_id))
            current = session.get(KnowledgeIndexJob, job.id)
            retry_job(session, current)
        self.assertEqual(self.run_job(job), 'succeeded')
        self.assertEqual(len(self.vectors), 1)

    def test_concurrent_replacements_and_soft_delete_reject_old_base_revision(self):
        first = self.submit(); self.run_job(first)
        a = self.submit('新规则。', document_id=first.document_id, base_revision=first.revision)
        b = self.submit('冲突规则。', document_id=first.document_id, base_revision=first.revision)
        self.assertEqual(self.run_job(a), 'succeeded')
        self.assertEqual(self.run_job(b), 'conflict')
        c = self.submit('不可恢复已删文档。', document_id=a.document_id, base_revision=a.revision)
        with Session(self.engine) as session, session.begin():
            session.get(KnowledgeDocument, a.document_id).deleted_at = datetime.now(timezone.utc)
        self.assertEqual(self.run_job(c), 'conflict')

    def test_lease_takeover_fences_old_worker(self):
        job = self.submit()
        def takeover(*args):
            self.index(*args)
            with Session(self.engine) as session, session.begin():
                current = session.get(KnowledgeIndexJob, job.id)
                current.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            self.assertIsNotNone(claim(self.engine, job.id))
        self.assertEqual(self.run_job(job, takeover), 'lease_lost')
        with Session(self.engine) as session:
            self.assertIsNone(session.get(KnowledgeDocument, job.document_id))

    def test_reconciliation_never_deletes_active_revision(self):
        active = self.submit(); self.run_job(active)
        failed = self.submit()
        def broken(*args): self.index(*args); raise RuntimeError('fail')
        self.run_job(failed, broken)
        preview = reconcile(self.engine, cleaner=self.clean)
        self.assertEqual(len(preview['candidates']), 1)
        self.assertFalse(preview['cleaned'])
        report = reconcile(self.engine, cleaner=self.clean, apply=True)
        self.assertEqual(len(report['cleaned']), 1)
        self.assertIn(active.revision, self.vectors)
        self.assertNotIn(active.revision, self.cleaned)

    def test_retry_uses_pinned_parse_instead_of_repeating_ocr(self):
        job=self.submit()
        def partial(*args):raise RuntimeError('vector timeout')
        self.assertEqual(self.run_job(job,partial),'failed')
        with Session(self.engine) as session, session.begin():
            retry_job(session,session.get(KnowledgeIndexJob,job.id))
        with patch('src.agent.knowledge_jobs.prepare_document',side_effect=AssertionError('parser repeated')):
            self.assertEqual(self.run_job(job),'succeeded')
