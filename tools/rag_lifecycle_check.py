"""Destructive fault checks restricted to the fresh sandbox created by rag_sandbox."""
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session
from api.knowledge_models import KnowledgeIndexJob
from api.models import KnowledgeDocument
from src.agent.knowledge_ingest import prepare_document
from src.agent.knowledge_jobs import make_job, run_job
from tools import rag_eval


def check_lifecycle(engine, actor, out):
    schema = os.environ.get('KNOWLEDGE_PG_SCHEMA','')
    if not schema.startswith('rag_eval_') or os.environ.get('MILVUS_DB_NAME') != schema:
        raise ValueError('Lifecycle checks require an explicit matching rag_eval_* sandbox')
    def submit(text):
        prepared = prepare_document('fault-check.md', text.encode())
        job = make_job(prepared=prepared, actor_id=actor)
        with Session(engine, expire_on_commit=False) as session, session.begin():
            session.add(job)
        return job
    common = '''
import os,sys,uuid
from dotenv import load_dotenv
load_dotenv('.env')
from sqlalchemy.orm import Session
from src.agent.knowledge_database import knowledge_engine
from src.agent.knowledge_jobs import claim,publish,run_job
from src.agent.knowledge_archive import cached_prepared
from src.agent.knowledge_ingest import prepare_document
from src.agent.knowledge_store import index_document
engine=knowledge_engine()
job_id=uuid.UUID(sys.argv[1])
'''
    results, created_ids = {}, []
    for phase in ('after_vectors','before_sql_commit'):
        job = submit('隔离崩溃恢复凭证：星河四二。')
        created_ids.append(job.document_id)
        child = common + '''
job=claim(engine,job_id)
prepared=cached_prepared(job,prepare_document)
index_document(job.document_id,job.revision,prepared.filename,prepared.chunks)
if sys.argv[2]=='before_sql_commit':
    session=Session(engine)
    session.begin()
    publish(session,job,prepared)
    session.flush()
os._exit(23)
'''
        process = subprocess.run([sys.executable,'-c',child,str(job.id),phase], timeout=120,
                                 capture_output=True, text=True)
        if process.returncode != 23:
            raise RuntimeError(f'Fault process failed before injection: {process.stderr[-500:]}')
        with Session(engine) as session, session.begin():
            assert session.get(KnowledgeDocument,job.document_id) is None
            current=session.get(KnowledgeIndexJob,job.id)
            assert current.status=='running'
            current.lease_until=datetime.now(timezone.utc)-timedelta(seconds=1)
        assert run_job(engine,job.id)=='succeeded'
        assert run_job(engine,job.id) is None
        from src.agent.memory.client import get_client
        rows=get_client().query(collection_name='agent_knowledge_v1',
            filter=f'revision == {json.dumps(str(job.revision))}',output_fields=['id'],limit=100,
            consistency_level='Strong')
        assert len(rows)==1, 'Retry created duplicate vector chunks'
        results[phase]={'process_exit':23,'uncommitted_document_hidden':True,'recovered':True,'vector_count':len(rows)}
    job=submit('隔离租约接管凭证：苍穹七七。')
    created_ids.append(job.document_id)
    ready,go=out/'lease.ready',out/'lease.go'
    child=common+'''
import time
from pathlib import Path
ready,go=Path(sys.argv[2]),Path(sys.argv[3])
def delayed(*args):
    index_document(*args)
    ready.write_text('ready')
    deadline=time.monotonic()+90
    while not go.exists():
        if time.monotonic()>deadline:raise TimeoutError('barrier timed out')
        time.sleep(.05)
result=run_job(engine,job_id,indexer=delayed,heartbeat=False)
assert result=='lease_lost',result
'''
    process=subprocess.Popen([sys.executable,'-c',child,str(job.id),str(ready),str(go)],
                             stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    try:
        deadline=time.monotonic()+120
        while not ready.exists():
            if process.poll() is not None or time.monotonic()>deadline:
                raise RuntimeError('Lease test child did not reach barrier')
            time.sleep(.1)
        with Session(engine) as session, session.begin():
            session.get(KnowledgeIndexJob,job.id).lease_until=datetime.now(timezone.utc)-timedelta(seconds=1)
        assert run_job(engine,job.id)=='succeeded'
        go.write_text('release')
        _,stderr=process.communicate(timeout=30)
        assert process.returncode==0,stderr[-500:]
        with Session(engine) as session:
            assert session.get(KnowledgeDocument,job.document_id).revision==job.revision
        results['lease_takeover']={'new_worker_published':True,'old_worker_fenced':True}
    finally:
        go.write_text('release')
        if process.poll() is None:
            process.terminate();process.wait(timeout=10)
    rag_eval.write_json(out/'faults.json',results)
    with Session(engine) as session, session.begin():
        for document_id in created_ids:
            document = session.get(KnowledgeDocument, document_id)
            if document is not None:
                document.deleted_at = datetime.now(timezone.utc)
    return results
