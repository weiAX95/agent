import unittest
import json
import os
from types import SimpleNamespace
from unittest.mock import patch
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from src.agent.chat_workflow import build_chat_workflow
from test_chat_workflow import make_services


class EvidenceAgent:
    def __init__(self, text):
        self.text = text
        self.updated = []
    async def astream(self, *args, **kwargs):
        yield {'type':'messages', 'data':(AIMessageChunk(content=self.text), {'langgraph_node':'model'})}
        yield {'type':'updates','data':{'model':{'messages':[AIMessage(id='last', content=self.text)]}}}
    async def aupdate_state(self, config, patch):
        self.updated.extend(patch['messages'])


class KnowledgeChatTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, text, hits):
        agent, tokens = EvidenceAgent(text), []
        async def token(value): tokens.append(value)
        with patch('src.agent.chat_workflow.search_knowledge_chunks', return_value=hits):
            workflow = build_chat_workflow(make_services([]))
            result = await workflow.ainvoke(dict(message='/knowledge 如何操作',session_id='s', user_id='u',
                                                  agent=agent, on_token=token))
        return result, tokens, agent

    async def test_invalid_citation_is_never_streamed_and_checkpoint_matches_final(self):
        hits = [dict(document_id='d', chunk_id='c', title='文档', page=1, text='必须停机')]
        result, tokens, agent = await self.execute('可以直接开机。[K9]', hits)
        self.assertNotIn('K9', ''.join(tokens))
        self.assertEqual(''.join(tokens), result['answer'])
        self.assertEqual(agent.updated[-1].content, result['answer'])
        self.assertEqual(agent.updated[-1].id, 'last')

    async def test_valid_citation_preserves_markdown(self):
        hits = [dict(document_id='d', chunk_id='c', title='文档', page=1, text='必须停机')]
        result, tokens, agent = await self.execute('**必须停机**。[K1]', hits)
        self.assertEqual(result['answer'], '**必须停机**。[K1]')
        self.assertEqual(''.join(tokens), result['answer'])
        self.assertFalse(agent.updated)

    async def test_no_result_cannot_become_confident_uncited_answer(self):
        result, tokens, agent = await self.execute('这肯定是正确的。', [])
        self.assertNotIn('肯定', ''.join(tokens))
        self.assertIn('无法', result['answer'])

    async def test_normal_research_claim_is_checked_before_streaming(self):
        class ResearchAgent(EvidenceAgent):
            async def astream(self, *args, **kwargs):
                yield {'type':'updates','data':{'model':{'messages':[AIMessage(content='',tool_calls=[
                    {'id':'research-1','name':'research','args':{'query':'春节安排'}}]) ]}}}
                yield {'type':'updates','data':{'tools':{'messages':[ToolMessage(
                    tool_call_id='research-1', name='research', content=json.dumps({'evidence':[
                        {'text':'工作日九点开放', 'document_id':'d','chunk_id':'c'}]}))]}}}
                async for event in super().astream(*args, **kwargs): yield event
        agent, tokens = ResearchAgent('春节肯定不开门。'), []
        async def token(value):tokens.append(value)
        with patch.dict(os.environ, {'KNOWLEDGE_VERIFY_CLAIMS':'1'}), patch(
                'src.agent.chat_workflow.model_support_review', return_value={'supported':False}):
            result = await build_chat_workflow(make_services([])).ainvoke(dict(
                message='春节开门吗',session_id='s',user_id='u',agent=agent,on_token=token))
        self.assertNotIn('肯定', ''.join(tokens))
        self.assertEqual(agent.updated[-1].content, result['answer'])
        self.assertFalse(result['answer_validation']['valid'])

    async def test_cancelled_buffer_does_not_save_unchecked_draft(self):
        import asyncio
        from dataclasses import replace
        restored = []
        class CancelAgent(EvidenceAgent):
            async def astream(self, *args, **kwargs):
                yield {'type':'messages','data':(AIMessageChunk(content='未校验草稿[K99]'),{'langgraph_node':'model'})}
                raise asyncio.CancelledError()
        async def restore(*args):restored.append(args[-1])
        async def token(value):raise AssertionError('Unchecked draft leaked')
        services = replace(make_services([]), restore_stopped_turn=restore)
        hits = [dict(document_id='d',chunk_id='c',title='文档',page=1,text='证据')]
        with patch('src.agent.chat_workflow.search_knowledge_chunks',return_value=hits):
            result = await build_chat_workflow(services).ainvoke(dict(message='/knowledge 问题',
                session_id='s',user_id='u',agent=CancelAgent(''),on_token=token))
        self.assertEqual(restored, [''])
        self.assertEqual(result['status'],'stopped')
