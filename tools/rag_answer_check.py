"""Small, opt-in real-model answer samples; never writes user memories or checkpoints."""
import asyncio
import json
import uuid
import time
from types import SimpleNamespace
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langgraph.checkpoint.memory import InMemorySaver
from src.agent.chat_workflow import ChatServices, build_chat_workflow
from src.agent.main import build_agent, _restore_stopped_turn, _discard_incomplete_turn
from src.agent.checkpoint import make_thread_id
from tools import rag_eval


async def sample_answers(dataset, out):
    selected = {'fact-03', 'no_answer-04', 'conflict-01'}
    agent = build_agent(InMemorySaver(), initialize=False)
    def prepare(message, session, user, agent, callbacks=None):
        return ({}, lambda _: [], {'messages':[{'role':'user','content':message}]},
                {'configurable':{'thread_id':make_thread_id(user,session)}},
                {'system_prompt': '你是一个带长期记忆能力的 Agent。普通对话直接回答；需要查证资料时使用 research。'
                 '只有实际检索到的资料可作为来源；知识库没有答案时明确说明，不编造出处。'
                 '用户此处询问的云杉实验室是虚构资料，不能用真实机构信息替代。'})
    async def noop(*args): pass
    def forbidden(*args, **kwargs): raise AssertionError('Answer evaluation must never write memories')
    services = ChatServices(prepare, lambda _:False, forbidden, forbidden, forbidden,
        SimpleNamespace(invoke=forbidden), forbidden, lambda content:content if isinstance(content,str) else '',
        _restore_stopped_turn, _discard_incomplete_turn)
    workflow = build_chat_workflow(services)
    rows = []
    for case in dataset['cases']:
        if case['case_id'] not in selected:
            continue
        for mode in ('normal','knowledge'):
            session, user = str(uuid.uuid4()), str(uuid.uuid4())
            activities, tokens = [], []
            usage = UsageMetadataCallbackHandler()
            async def token(value): tokens.append(value)
            async def activity(*args): activities.append(args)
            query = '云杉实验室：' + case['query']
            if mode == 'knowledge': query = '/knowledge ' + query
            started = time.monotonic()
            try:
                result = await workflow.ainvoke(dict(message=query, session_id=session,user_id=user,
                    agent=agent,on_token=token,on_activity=activity,callbacks=[usage]))
                snapshot = await agent.aget_state({'configurable':{'thread_id':make_thread_id(user,session)}})
                tool_outputs = [m.content for m in snapshot.values.get('messages',[])
                                if getattr(m,'type',None)=='tool']
                rows.append({'case_id':case['case_id'], 'mode':mode, 'answer':result['answer'],
                    'stream_matches_final': ''.join(tokens) == result['answer'],
                    'validation':result.get('answer_validation',{}),
                    'activities':activities, 'tool_outputs':tool_outputs,
                    'usage': usage.usage_metadata, 'status':'ok'})
            except Exception as exc:
                rows.append({'case_id':case['case_id'], 'mode':mode,'status':'error',
                             'error_type':type(exc).__name__, 'error':str(exc)[:500]})
            rows[-1]['duration_ms'] = round((time.monotonic() - started) * 1000, 2)
            rag_eval.write_json(out / f"answer-{case['case_id']}-{mode}.json", rows[-1])
            print(json.dumps({'answer_case':case['case_id'], 'mode':mode,'status':rows[-1]['status']}), flush=True)
    return rows
