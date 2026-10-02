import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from fastapi import Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from src.agent.main import chat

from api.db import Base, check_db_connection, engine, get_db
from api.models import ChatLog
from api.routers import paths, tasks, topics


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("注册的表:", list(Base.metadata.tables.keys()))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(lifespan=lifespan)  # 关键：lifespan必须在这里传进去，否则永远不会执行

app.include_router(paths.router)
app.include_router(topics.router)
app.include_router(tasks.router)


class ChatRequest(BaseModel):
    message: str
    session_id: str
    user_id: str


@app.get("/health")
async def health():
    db_ok = await check_db_connection()
    return {"status": "ok", "database": "connected" if db_ok else "disconnected"}


@app.post("/chat")
async def chat_api(request: ChatRequest, db: AsyncSession = Depends(get_db)):
    answer = chat(
        message=request.message,
        user_id=request.user_id,
        session_id=request.session_id,
    )
    print(
        f"Request: {request.message}, Session ID: {request.session_id}, Answer: {answer}"
    )

    log = ChatLog(
        user_id=request.user_id,
        session_id=request.session_id,
        message=request.message,
        answer=answer,
    )
    db.add(log)
    await db.commit()

    return {"answer": answer}
