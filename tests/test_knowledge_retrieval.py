import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from src.agent import knowledge_store as store


class RetrievalBoundsTests(unittest.TestCase):
    def test_expands_window_to_1000_filters_old_revisions_and_reports_work(self):
        valid = {'entity':{'document_id':'live','revision':'new','chunk_index':900,'filename':'doc',
                 'page':1,'text':'有效事实'},'id':'live-chunk','distance':.8}
        stale = {'entity':{'document_id':'live','revision':'old','chunk_index':0,'filename':'doc',
                 'page':1,'text':'旧事实'},'id':'old-chunk','distance':.9}
        class Client:
            def has_collection(self,*args,**kwargs):return True
            def search(self, *, limit, **kwargs):
                hits = [stale]*limit
                if limit == 1000:hits[900] = valid
                return [hits]
        with patch.dict(os.environ,{'KNOWLEDGE_RETRIEVAL_MODE':'vector','KNOWLEDGE_EXPAND_NEIGHBORS':'0'}), \
             patch.object(store,'get_client',return_value=Client()), \
             patch.object(store,'get_embed_model',return_value=SimpleNamespace(embed_query=lambda q:[0.])), \
             patch.object(store,'_active_revisions',return_value={'live':('new','doc')}):
            hits=store.search_knowledge_chunks('问题')
        self.assertEqual([h['text'] for h in hits],['有效事实'])
        trace=store.last_retrieval.get()
        self.assertEqual(trace['windows'],[25,125,625,1000])
        self.assertEqual(trace['inactive_filtered'],1774)
        self.assertEqual(trace['returned'],1)

    def test_invalid_limit_is_rejected_before_any_resource_initialization(self):
        with patch.object(store,'get_client',side_effect=AssertionError('should stay offline')):
            with self.assertRaises(ValueError):store.search_knowledge_chunks('q',limit=0)

    def test_hybrid_can_recover_via_live_sql_candidates_when_vector_window_is_full(self):
        stale={'entity':{'document_id':'live','revision':'old','page':1,'text':'旧事实'},
               'id':'old','distance':.9}
        client=SimpleNamespace(has_collection=lambda *a,**k:True,
                               search=lambda **k:[[stale]*k['limit']])
        lexical=[{'document_id':'live','revision':'new','chunk_id':'new','page':1,
                  'text':'预约需要两天','title':'doc','score':None}]
        with patch.dict(os.environ,{'KNOWLEDGE_RETRIEVAL_MODE':'hybrid','KNOWLEDGE_EXPAND_NEIGHBORS':'0'}), \
             patch.object(store,'get_client',return_value=client), \
             patch.object(store,'get_embed_model',return_value=SimpleNamespace(embed_query=lambda q:[0.])), \
             patch.object(store,'_active_revisions',return_value={'live':('new','doc')}), \
             patch('src.agent.knowledge_catalog.lexical_candidates',return_value=lexical):
            hits=store.search_knowledge_chunks('预约')
        self.assertEqual(hits[0]['chunk_id'],'new')
        self.assertTrue(store.last_retrieval.get()['vector_incomplete'])
