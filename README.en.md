# LLM Agent

[简体中文](README.md) · [Project progress (Chinese)](docs/TODO.md)

An experimental chat agent built with FastAPI and LangChain / LangGraph, exploring streaming responses, PostgreSQL conversation recovery, and long-term memory with Milvus.

> **Status: experimental.** Offline regression tests and partial integration verification are documented. End-to-end integration of real models, Milvus, and the complete chat API remains unfinished. This is not a ready-to-deploy public service.

## Problems explored

| Problem | Current approach |
| --- | --- |
| Recover context after a restart | PostgreSQL checkpoints |
| Separate users and conversations | Stable composite conversation identities |
| Concurrent requests in one conversation | PostgreSQL transaction-level advisory locks |
| Streaming and failure recovery | SSE events and targeted offline regressions |
| Save and retrieve long-term memory | Milvus and a local embedding model |

## Quick start

Use Python 3.12. Prepare PostgreSQL, Milvus (default: `localhost:19530`), and the local embedding model at `~/models/bge-base-zh-v1.5`. The tools module connects to Milvus and loads the embedding model during import; missing dependencies may prevent the API from starting.

```bash
git clone https://github.com/weiAX95/agent.git
cd agent
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Create a local `.env` file at the repository root:

```dotenv
DATABASE_URL=postgresql+asyncpg://agent:your_password@localhost:5432/agent
OPENAI_API_KEY=your_key
OPENAI_BASE_URL=https://your-model-api.example/v1
OPENAI_MODEL=your_model
```

Authentication also requires these values. The HS256 signing and verification keys should be the same high-entropy random secret.

```dotenv
JWT_SIGNING_KEY=replace_with_a_random_secret
JWT_VERIFYING_KEY=replace_with_the_same_random_secret
JWT_ISSUER=agent-api
JWT_AUDIENCE=agent-web
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=15
```

Replace placeholders locally. Do not commit real credentials. The database account needs permission to create checkpoint tables on first startup. [checkpoint.py](src/agent/checkpoint.py) converts the SQLAlchemy asyncpg URL into a psycopg connection string for the same PostgreSQL database.
The API requires JWT settings at startup; keep real secrets out of Git.

```bash
python -m uvicorn api.server:app --host 127.0.0.1 --port 8000 --reload
```

First startup initializes the API tables and LangGraph checkpoint tables. A PostgreSQL advisory lock serializes initialization across API workers.

```bash
curl http://127.0.0.1:8000/health
```

Register or log in first, then send the returned access token to `/api/chat`:

```bash
curl -X POST http://127.0.0.1:8000/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo","password":"replace_with_a_strong_password"}'

# Copy access_token from the response. Existing users can call /api/auth/login.
ACCESS_TOKEN='paste_access_token_here'
# Create a session and copy data.id from the response.
curl -X POST http://127.0.0.1:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -d '{"title":"First conversation"}'
SESSION_ID='paste_session_uuid_here'
# Reuse request_id only when retransmitting exactly the same request.
REQUEST_ID=$(python -c 'import uuid; print(uuid.uuid4())')
curl -N -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -d "{\"session_id\":\"${SESSION_ID}\",\"request_id\":\"${REQUEST_ID}\",\"message\":\"Hello\"}"
```

**API contract change (2026-10-26):** create a session before calling `/api/chat`, and supply `request_id`. The existing React frontend has not yet been adapted. This delivery covers backend persistence, not the history UI; see the [change map and verification](docs/session_storage_delivery.md).

## Engineering details

- `/api/chat` streams SSE events such as `token`, `usage`, `done`, `stopped`, and `error`. A `done` event contains the complete answer in `data.answer`.
- Token usage comes from the model provider through LangChain. Missing usage is marked unavailable rather than estimated from character counts.
- The client sends POST JSON with `fetch` and reads the response stream; native browser `EventSource` cannot send this POST body.
- [main.py](src/agent/main.py) uses `make_thread_id(user_id, session_id)` to produce stable, unambiguous checkpoint identities. Reusing a session ID across different users does not accidentally share short-term history.
- Startup creates the PostgreSQL pool and agent; shutdown releases the pool. Each turn submits only the current user message. Recalled content is injected into a dynamic system prompt rather than accumulated in persisted message history. Agent replies and tool messages are persisted by checkpoints. `ChatLog` records question/answer pairs and does not restore checkpoint state.
- `/api/chat` validates session ownership and acquires a session-level advisory lock from a dedicated pool for the full response lifetime. Overlapping requests for the same session return 409. User/assistant placeholders are committed before generation; terminal status and compatible `ChatLog` are committed before `done`/`stopped`. The request ID prevents duplicate generation on retransmission.
- The [request-scoped chat graph](src/agent/chat_workflow.py) coordinates recall, answering, and memory updates for both synchronous and POST SSE calls. The inner agent retains PostgreSQL checkpoints. Post-answer memory failures are logged while the completed answer is returned; see the [migration record](docs/chat_graph_migration.md).
- `done` and `stopped` events include `chat_log_id`. The separate React frontend in `/Users/zhangweiwei/Desktop/code/web/my-app` can submit answer or memory feedback to `POST /api/feedback`; see the [review and regression workflow](docs/feedback_regression.md). That frontend is outside this Git repository.

## Verification

With temporary conversation content:

1. Send two related messages as user A in session S and check context continuity.
2. Restart the API and continue A/S; check persisted checkpoint tables and recovered context.
3. B/S must return 404; create a separate session T for A and confirm it does not read A/S short-term history.

Avoid long-term memory save/recall confounding checkpoint isolation tests. Targeted offline regressions:

```bash
python -m unittest discover -s tests -p 'test_checkpoint_flow.py'
python -m unittest discover -s tests -p 'test_memory_update.py'
python -m unittest discover -s tests -p 'test_recall_routing.py'
python -m unittest discover -s tests -p 'test_stream_recovery.py'
python -m unittest discover -s tests -p 'test_chat_workflow.py'
python -m unittest discover -s tests -p 'test_feedback_api.py'
python -m unittest discover -s tests -p 'test_feedback_loop.py'
python -m unittest discover -s tests -p 'test_chat_storage.py'
python -m unittest discover -s tests -p 'test_chat_persistence.py'
python -m unittest discover -s tests -p 'test_chat_api.py'
```

`test_stream_recovery.py` uses fake models to cover SSE generation failure, checkpoint cleanup, same-conversation retries, and memory-update failures after a complete answer. Other scripts, including `test_gemini.py` and `test_search.py`, may access external services during import. Use targeted commands instead of unfiltered test discovery.

[GitHub Actions](.github/workflows/offline-regression.yml) runs these ten offline suites on pushes and pull requests, without connecting to real models, PostgreSQL, or Milvus.

**Existing repository verification record — not a full acceptance test of current authentication and feedback:** checkpoint initialization was exercised in a temporary schema on real PostgreSQL. A fake chat model completed two turns; after closing and recreating the pool, four human/AI messages were recovered. The temporary schema was removed. FastAPI lifespan startup and `/health` returned HTTP 200 with a connected database. Two real database sessions confirmed that a second request for the same advisory lock waits for the first transaction to commit. Full `/api/chat`, real-model/Milvus integration, cross-user isolation, and concurrent load still require integration testing.

## Known limitations

- `/api/chat` derives identity from the JWT `sub` claim, and path, topic, and task APIs check ownership. These changes have not passed two-user end-to-end acceptance. The standalone graph example still needs trusted identity binding for memory tools; composite thread IDs alone do not establish project-wide access control.
- Old `InMemorySaver` history is not migrated; new deployments start with new checkpoints.
- Lock wait timeouts, high-concurrency latency, and multi-process behavior need load testing. Checkpoint writes and `ChatLog` commits use separate connections and transactions: checkpoint state may advance even when log persistence fails. Compensation or retry behavior remains to be defined.
- [graph.py](src/agent/graph.py) is a separate learning entry point and is not unified with the API persistence flow. Its examples are not acceptance evidence for `/api/chat`.

## Further reading

The following documents are in Chinese:

- [Issues and improvements](docs/TODO.md)
- [Automatic recall notes](docs/auto_recall.md)
- [Learning progress roadmap](docs/learning_progress_roadmap.md)
- [Agent learning roadmap](docs/agent_learning_roadmap.md)

Code entry points: [API](api/server.py), [agent](src/agent/main.py), [memory tools](src/agent/tools.py), and [standalone graph example](src/agent/graph.py).
