# 模块职责：复用有界同步数据库连接池，供后台线程和 worker 使用，并在关闭时释放连接。
"""Bounded synchronous DB resources used from worker threads, never an event loop."""
# 导入 os，用于读取环境变量并执行必要的文件系统操作。
import os
# 导入 re，用于识别关键词、标题、引用编号或合法配置值。
import re
# 从 functools 导入 lru_cache，供下方复用已初始化资源或内容指纹。
from functools import lru_cache
# 从 sqlalchemy 导入 create_engine，供下方构造数据库字段与参数化查询。
from sqlalchemy import create_engine
# 从 sqlalchemy.engine 导入 make_url，供下方相关数据结构与业务步骤。
from sqlalchemy.engine import make_url
# 从 sqlalchemy.orm 导入 sessionmaker，供下方声明 ORM 字段或管理短会话。
from sqlalchemy.orm import sessionmaker


# 缓存指定数量的资源/文件签名计算结果，避免重复创建连接池或反复读取模型权重。
@lru_cache(maxsize=1)
# 定义函数：延迟创建并缓存同步 PostgreSQL 引擎，限制连接、等待和语句执行时间。
def knowledge_engine():
    # 从 api.config 导入 settings，供下方相关数据结构与业务步骤。
    from api.config import settings
    # 保留数据库地址各字段，只把异步驱动改为同步 psycopg，供 worker 线程调用。
    url = make_url(settings.database_url).set(drivername='postgresql+psycopg')
    # 准备「SQL schema 名称或 Milvus 字段结构」：读取环境配置（未设置时采用代码中的默认值）。
    schema = os.getenv('KNOWLEDGE_PG_SCHEMA', 'public')
    # schema 会进入连接启动参数，所以先限制为安全的 PostgreSQL 标识符格式。
    if not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', schema):
        # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
        raise ValueError('Invalid knowledge PostgreSQL schema')
    # 创建容量为 4、最多额外 2 个连接的池，借连接最多等待 5 秒；不是每题重新建立连接。
    return create_engine(url, pool_size=4, max_overflow=2, pool_timeout=5,
                         # 设置「借出连接前的存活检测」参数，使当前调用使用明确的配置或元数据。
                         pool_pre_ping=True, connect_args={'connect_timeout': 5,
                         # 写入「数据库会话启动选项」字段，与同一条记录的其他字段共同组成结果。
                         'options': f'-c statement_timeout=10000 -c search_path={schema}'})


# 定义函数：创建一个使用共享连接池的短期 SQL 会话；调用方负责上下文和事务。
def knowledge_session():
    # 借用共享连接池创建会话，提交后保留已加载属性，避免访问时触发意外的延迟查询。
    return sessionmaker(knowledge_engine(), expire_on_commit=False)()


# 定义函数：仅关闭已经初始化的连接池，并清除引擎缓存，避免关闭时反而新建连接。
def close_knowledge_database():
    # 先判断资源是否已初始化，关闭流程不应反而触发一次新建。
    if knowledge_engine.cache_info().currsize:
        # 释放缓存引擎的连接池资源。
        knowledge_engine().dispose()
        # 清除已关闭引擎的缓存，使之后初始化能使用当前配置。
        knowledge_engine.cache_clear()
