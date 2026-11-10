"""Run synthetic retrieval experiments in a fresh PostgreSQL schema and Milvus database.

Creates only uniquely named rag_eval_* resources; drops those exact resources on exit.
No chat model is called. Raw results remain under ignored output/rag/.
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--scale', action='store_true', help='Inject and clean 1000 stale vectors in this sandbox')
    parser.add_argument('--profile', choices=['vector_v1','hybrid_v1','vector_v2','hybrid_v2','hybrid_v2_neighbors'])
    parser.add_argument('--faults-only', action='store_true', help='Skip retrieval profiles; run only isolated process-fault checks')
    parser.add_argument('--faults', action='store_true', help='Run real process-crash and lease-takeover checks in the sandbox')
    parser.add_argument('--answers-only', action='store_true', help='Run vector-v2 retrieval plus the six answer samples')
    parser.add_argument('--answers', action='store_true', help='Call the existing model for six fixed answer samples')
    parser.add_argument('--keep', action='store_true', help='Retain the isolated services for manual answer review')
    args = parser.parse_args(argv)
    if args.faults_only and (args.scale or args.answers or args.answers_only or args.profile):
        parser.error('--faults-only cannot be combined with retrieval/answer options')
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    import psycopg
    from psycopg import sql
    from pymilvus import MilvusClient
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from api.config import settings
    from src.agent.checkpoint import checkpoint_dsn
    from tools import rag_eval
    name = 'rag_eval_' + uuid.uuid4().hex[:16]
    out = args.out or ROOT / 'output/rag' / name
    out.mkdir(parents=True, exist_ok=False)
    os.environ['KNOWLEDGE_ARCHIVE_DIR'] = str(out.resolve() / 'archive')
    os.environ['KNOWLEDGE_PG_SCHEMA'] = name
    os.environ['MILVUS_DB_NAME'] = name
    os.environ['KNOWLEDGE_OCR'] = '0'
    os.environ['KNOWLEDGE_PDF_LAYOUT'] = '0'
    uri = os.getenv('MILVUS_URI', 'http://localhost:19530')
    admin = MilvusClient(uri=uri, timeout=10)
    schema_created = db_created = False
    engine = None
    cleanup = {}
    try:
        with psycopg.connect(checkpoint_dsn(settings.database_url), autocommit=True, connect_timeout=5) as conn:
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(name)))
        schema_created = True
        admin.create_database(name, timeout=10)
        db_created = True
        from api.base import Base
        from api.models import User, KnowledgeDocument
        from api.knowledge_models import KnowledgeIndexJob, KnowledgeChunkRecord, KnowledgeAuditEvent
        from src.agent.knowledge_database import knowledge_engine, close_knowledge_database
        from src.agent.knowledge_ingest import prepare_document
        from src.agent.knowledge_jobs import make_job, run_job, reconcile
        from src.agent.memory.client import close_memory_resources
        from src.agent.knowledge_store import search_knowledge_chunks
        engine = knowledge_engine()
        Base.metadata.create_all(engine, tables=[User.__table__, KnowledgeDocument.__table__,
            KnowledgeIndexJob.__table__, KnowledgeChunkRecord.__table__, KnowledgeAuditEvent.__table__])
        actor = uuid.uuid4()
        with Session(engine, expire_on_commit=False) as session, session.begin():
            session.add(User(id=actor, username='synthetic-rag-evaluator', hashed_password='not-a-login'))
        dataset_path = ROOT / 'evals/rag/dataset.json'
        dataset = rag_eval.load_dataset(dataset_path)
        docs = dataset['documents']
        document_ids, revisions, runs, summaries = {}, {}, {}, {}
        previous_chunking = None
        profiles = [('vector_v1','vector','1','0'), ('hybrid_v1','hybrid','1','0'),
                    ('vector_v2','vector','2','0'), ('hybrid_v2','hybrid','2','0'),
                    ('hybrid_v2_neighbors','hybrid','2','1')]
        if args.faults_only:
            profiles = []
        elif args.answers_only:
            profiles = [('vector_v2','vector','2','0')]
        elif args.profile:
            profiles = [p for p in profiles if p[0] == args.profile]
        def catalog():
            with Session(engine) as session:
                rows = session.execute(select(KnowledgeDocument).where(KnowledgeDocument.deleted_at.is_(None))).scalars()
                return [dict(document_id=str(r.id), revision=str(r.revision), sha256=r.sha256) for r in rows]
        for profile, mode, chunking, neighbors in profiles:
            os.environ.update(KNOWLEDGE_RETRIEVAL_MODE=mode, KNOWLEDGE_CHUNKING_VERSION=chunking,
                              KNOWLEDGE_EXPAND_NEIGHBORS=neighbors)
            if previous_chunking != chunking:
                for doc in docs:
                    prepared = prepare_document(Path(doc['path']).name, (dataset_path.parent / doc['path']).read_bytes())
                    job = make_job(prepared=prepared, actor_id=actor, document_id=document_ids.get(doc['key']),
                                   base_revision=revisions.get(doc['key']))
                    with Session(engine, expire_on_commit=False) as session, session.begin():
                        session.add(job)
                    status = run_job(engine, job.id)
                    if status != 'succeeded':
                        with Session(engine) as session:
                            error = session.get(KnowledgeIndexJob, job.id).error
                        raise RuntimeError(f'Synthetic indexing failed: {status}: {error}')
                    document_ids[doc['key']], revisions[doc['key']] = job.document_id, job.revision
                previous_chunking = chunking
            bindings = {'environment':name, 'documents':{doc['key']:{
                'document_id':str(document_ids[doc['key']]), 'revision':str(revisions[doc['key']]),
                'sha256':doc['sha256']} for doc in docs}}
            _, _, runtime, _ = rag_eval.live_backend()
            from src.agent.knowledge_store import last_retrieval
            run = rag_eval.run_dataset(dataset, bindings, search=search_knowledge_chunks,
                                       catalog_loader=catalog, runtime=runtime, trace_reader=last_retrieval.get)
            report = rag_eval.build_report(run)
            runs[profile] = run
            summaries[profile] = report['metrics']
            rag_eval.write_json(out / f'{profile}.json', run)
            rag_eval.write_json(out / f'{profile}.report.json', report)
            rag_eval.write_json(out / f'{profile}.reviews.json', rag_eval.review_template(run))
            rag_eval.write_json(out / f'{profile}.bindings.json', bindings)
            (out / f'{profile}.report.md').write_text(rag_eval.markdown_report(report))
            print(json.dumps({'profile':profile, 'metrics':report['metrics']}), flush=True)
            reference = next(iter(runs))
            if profile != reference:
                rag_eval.write_json(out / f'{profile}.comparison.json', rag_eval.compare_runs(runs[reference], run))
        if args.scale:
            from tools.rag_scale_check import check_scale
            check_scale(engine, document_ids[docs[0]['key']], out)
        if args.answers or args.answers_only:
            import asyncio
            from tools.rag_answer_check import sample_answers
            os.environ.update(KNOWLEDGE_RETRIEVAL_MODE='vector', KNOWLEDGE_EXPAND_NEIGHBORS='0',
                              KNOWLEDGE_VERIFY_CLAIMS='1')
            answers = asyncio.run(sample_answers(dataset, out))
            rag_eval.write_json(out / 'answers.json', answers)
        if args.faults or args.faults_only:
            from tools.rag_lifecycle_check import check_lifecycle
            check_lifecycle(engine, actor, out)
        if not args.faults_only:
            # Exercise replacement and soft-delete authority after the fixed experiments.
            prepared = prepare_document('deletion-check.md', '隔离生命周期标记：删除后不得检索。'.encode())
            deletion = make_job(prepared=prepared, actor_id=actor)
            with Session(engine, expire_on_commit=False) as session, session.begin():
                session.add(deletion)
            assert run_job(engine, deletion.id) == 'succeeded'
            target_id = deletion.document_id
            with Session(engine, expire_on_commit=False) as session, session.begin():
                target = session.get(KnowledgeDocument, target_id)
                target.deleted_at = datetime.now(timezone.utc)
            deleted_hits = search_knowledge_chunks('隔离生命周期标记是什么？')
            assert all(h['document_id'] != str(target_id) for h in deleted_hits), 'Deleted document leaked'
            clean = reconcile(engine, apply=True)
            rag_eval.write_json(out / 'lifecycle.json', {'deleted_document_hidden':True, 'cleanup':clean})
        rag_eval.write_json(out / 'summary.json', {'environment':name, 'profiles':summaries,
            'manual_review':'not_run', 'no_chat_model_calls':not (args.answers or args.answers_only), 'lifecycle':'passed'})
        print('Results:', out, flush=True)
        return 1 if any(m['error_count'] for m in summaries.values()) else 0
    finally:
        if engine is not None:
            from src.agent.memory.client import close_memory_resources
            from src.agent.knowledge_database import close_knowledge_database
            close_memory_resources()
            close_knowledge_database()
        if not args.keep:
            if db_created:
                try:
                    isolated = MilvusClient(uri=uri, db_name=name, timeout=10)
                    try:
                        for collection in isolated.list_collections():
                            isolated.drop_collection(collection)
                    finally:
                        isolated.close()
                    admin.drop_database(name)
                    cleanup['milvus'] = 'removed'
                except Exception as exc:
                    cleanup['milvus'] = type(exc).__name__
            if schema_created:
                try:
                    with psycopg.connect(checkpoint_dsn(settings.database_url), autocommit=True, connect_timeout=5) as conn:
                        conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(name)))
                    cleanup['postgres'] = 'removed'
                except Exception as exc:
                    cleanup['postgres'] = type(exc).__name__
        else:
            cleanup = {'retained_environment':name}
        admin.close()
        rag_eval.write_json(out / 'cleanup.json', cleanup)


if __name__ == '__main__':
    raise SystemExit(main())
