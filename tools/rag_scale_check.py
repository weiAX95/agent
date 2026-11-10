"""A bounded real-Milvus 1000-stale-vector check in the disposable sandbox."""
import json
import os
import time
import uuid
from src.agent.knowledge_store import search_knowledge_chunks, get_client, get_embed_model, last_retrieval, KNOWLEDGE_COLLECTION
from src.agent.knowledge_jobs import reconcile
from tools import rag_eval


def check_scale(engine, document_id, out):
    schema=os.getenv('KNOWLEDGE_PG_SCHEMA','')
    if not schema.startswith('rag_eval_') or os.getenv('MILVUS_DB_NAME')!=schema:
        raise ValueError('Scale checks require a matching disposable sandbox')
    os.environ['KNOWLEDGE_RETRIEVAL_MODE']='vector'
    os.environ['KNOWLEDGE_EXPAND_NEIGHBORS']='0'
    query='设备预约需要提前多久？'
    vector=get_embed_model().embed_query(query)
    revision=uuid.uuid4()
    rows=[{'id':str(uuid.uuid5(revision,str(i))), 'document_id':str(document_id),
           'revision':str(revision),'chunk_index':i,'filename':'obsolete-synthetic',
           'page':0,'text':'过时的虚构预约事实','vector':vector} for i in range(1000)]
    get_client().upsert(collection_name=KNOWLEDGE_COLLECTION,data=rows)
    capped=False
    started=time.monotonic()
    try:
        search_knowledge_chunks(query)
    except RuntimeError:
        capped=True
    before=dict(last_retrieval.get())
    assert capped and before.get('candidate_cap_reached'), 'Candidate cap was not distinguished from no answer'
    cleanup=reconcile(engine,apply=True,scan_vectors=True)
    hits=search_knowledge_chunks(query)
    assert any(h['document_id']==str(document_id) for h in hits), 'Live result not recovered after cleanup'
    result={'stale_vector_count':1000,'before':before,'after':dict(last_retrieval.get()),
            'cap_is_not_reported_as_no_answer':True,'live_result_recovered':True,'cleanup':cleanup,
            'duration_ms':round((time.monotonic()-started)*1000,2)}
    rag_eval.write_json(out/'scale.json',result)
    return result
