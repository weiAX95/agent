from app.core.config import settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# echo=True 会打印所有SQL，调试完记得关掉
engine = create_async_engine(
    settings.database_url, echo=False, pool_size=5, max_overflow=10
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    """FastAPI依赖注入用：每个请求拿到一个独立session，请求结束自动关闭"""
    async with AsyncSessionLocal() as session:
        yield session
