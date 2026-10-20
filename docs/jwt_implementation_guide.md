# JWT 认证集成实施指南

> 本文原为从零集成 JWT 的实施草案，下面的代码块与行号保留作设计参考，不能当作当前源码逐行执行。当前代码已有后端认证实现，但双用户隔离和生产密钥管理仍待验收。请先看下表与 [JWT 当前状态](jwt_auth.md)，再按剩余验收项推进。

| 范围 | 当前代码状态 | 验收状态 |
| --- | --- | --- |
| PyJWT、Argon2、JWT 配置、签发与验证 | 已在 `requirements.txt`、`api/config.py`、`api/security.py`、`api/auth.py` 中编写 | 缺独立认证测试和真实启动验证 |
| 用户表、注册/登录、`/api/auth/me` | 已在 `api/models.py`、`api/routers/auth.py` 中编写 | 缺真实数据库与双用户端到端验证 |
| `/chat` 认证身份、路径/主题/任务归属 | 已在 API 路由中编写 | 缺逐路由越权回归和完整 SSE 联调 |
| 前端登录、令牌生命周期、生产密钥轮换 | 此仓库无已验收实现 | 待设计与实施 |

---

## 前置决策：身份来源（必须先做）

本地原型已采用自建登录；生产方案仍需在自建身份和 OIDC 之间作决定。两条路线后续差异很大：

| 方案 | 适用场景 | 令牌签发方 | 项目复杂度 |
|------|----------|-----------|-----------|
| **OIDC（推荐上线）** | 多用户、长期维护 | 外部 IdP（Auth0 / Keycloak / 自建） | 低，不用管理密码 |
| **自建登录（学习用）** | 个人学习、离线环境 | 本项目自己签发 | 高，需管密码哈希、令牌生命周期 |

**当前选择**：本地原型使用自建登录。下文保留这一方案的原始实施步骤；是否迁移 OIDC 应在部署前根据实际身份需求决定。

---

## Phase 1：基础设施（无业务逻辑变更）

### Step 1.1 — 声明 PyJWT 依赖

**文件**：`requirements.txt`

**操作**：在依赖列表中添加：

```
PyJWT==2.10.1
```

**验证**：`pip install PyJWT` 后 `python -c "import jwt; print(jwt.__version__)"` 输出 2.10.1。

---

### Step 1.2 — 扩展配置模型

**文件**：`api/config.py`

**原始基线（本地代码现已扩展）**：
```python
class Settings(BaseSettings):
    database_url: str
```

**修改为**：
```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str

    # JWT 配置
    jwt_signing_key: str          # HS256 签名密钥（开发环境从 .env 读取）
    jwt_verifying_key: str        # HS256 验证密钥（与签名密钥相同）
    jwt_issuer: str               # 令牌签发者标识，如 "agent-api"
    jwt_audience: str             # 预期接收方标识，如 "agent-web"
    jwt_access_token_expire_minutes: int = 15

    class Config:
        env_file = ".env"
        extra = "ignore"
```

**对应 `.env` 新增**（不提交到 Git）：
```dotenv
JWT_SIGNING_KEY=your-256-bit-random-secret-here
JWT_VERIFYING_KEY=your-256-bit-random-secret-here
JWT_ISSUER=agent-api
JWT_AUDIENCE=agent-web
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=15
```

**生成开发密钥**（在终端执行，结果粘贴到 `.env`）：
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

**验收标准**：`Settings()` 实例化成功，且 `settings.jwt_signing_key` 非空。

---

### Step 1.3 — 启动时配置校验（快速失败）

**文件**：`api/server.py`

**原始问题（本地代码已改）**：早期草案中 JWT 配置直接用 `os.getenv(..., "")` 读取。本地 `Settings` 现要求这些字段，`lifespan` 也检查非空值；仍需真实启动验证。

**操作**：在 `lifespan` 函数开头添加配置验证：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时强制校验 JWT 配置，拒绝空值启动
    if not settings.jwt_verifying_key:
        raise RuntimeError(
            "JWT_VERIFYING_KEY 未配置。请在 .env 中设置非空密钥。"
        )
    if not settings.jwt_issuer:
        raise RuntimeError("JWT_ISSUER 未配置。")
    if not settings.jwt_audience:
        raise RuntimeError("JWT_AUDIENCE 未配置。")

    pool = None
    try:
        ...
```

**同时删除** `server.py` 顶部的旧读取方式：
```python
# 删除这三行，改用 settings
# JWT_VERIFYING_KEY = os.getenv("JWT_VERIFYING_KEY", "")
# JWT_ISSUER = os.getenv("JWT_ISSUER", "")
# JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "")
```

**验收标准**：注释掉 `.env` 中任一 JWT 配置后启动服务，进程立即退出并给出明确错误信息。

---

## Phase 2：核心修复 — 身份一致性（最关键）

### Step 2.1 — 从 ChatRequest 移除 user_id

**文件**：`api/server.py`

**原始基线（本地代码已移除 `user_id`）**：
```python
class ChatRequest(BaseModel):
    message: str
    session_id: str
    user_id: str  # ← 客户端可伪造
```

**修改为**：
```python
class ChatRequest(BaseModel):
    message: str
    session_id: str
    # user_id 已从请求体移除，身份完全由 token sub 决定
```

---

### Step 2.2 — 统一使用认证身份

**文件**：`api/server.py`

**原始问题（本地代码已改）**：早期草案中锁使用认证身份，后续操作仍使用请求体身份。当前本地 `chat_api` 已统一使用认证用户；下面的行号仅为历史改造清单，不再对应当前文件。

**修改点清单**（逐行修改）：

| 行号 | 当前代码 | 改为 |
|------|---------|------|
| 157 | `print("session_id:", request.session_id, "user_id:", request.user_id)` | `print("session_id:", request.session_id, "user_id:", user_id)` |
| 163 | `user_id=request.user_id` | `user_id=user_id` |
| 178 | `request.user_id` | `user_id` |
| 192 | `user_id=request.user_id` | `user_id=user_id` |
| 230 | `request.user_id` | `user_id` |
| 260 | `request.user_id` | `user_id` |

**修改后的 chat_api 签名**（不变，已有 `current_user` 依赖）：
```python
async def chat_api(
    request: ChatRequest,
    http_request: Request,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = current_user["user_id"]  # ← 唯一可信身份来源
    # 后续所有 user_id 引用统一使用这个变量
```

**验收标准**：发送请求时 body 中不包含 `user_id`，服务正常响应；在 body 中放入伪造的 `user_id` 字段，服务忽略它（Pydantic 会报 extra field 或直接忽略）。

---

## Phase 3：保护所有业务路由

### Step 3.1 — 学习路径（paths.py）

**文件**：`api/routers/paths.py`

**需要三处修改**：

**① 添加导入和依赖**：
```python
from api.server import get_current_user  # 或抽到公共模块避免循环导入
```

> **注意**：`get_current_user` 目前定义在 `server.py` 中。如果 `paths.py` 直接导入会导致循环导入（`server.py` 已导入 `paths.py`）。**解决方案**：将 `get_current_user` 和 `bearer` 抽取到独立模块 `api/auth.py`。

**② 所有端点添加认证依赖**：
```python
@router.get("")
async def list_paths(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),  # ← 新增
):
    stmt = select(LearningPath).where(
        LearningPath.deleted_at.is_(None),
        LearningPath.owner_id == current_user["user_id"],  # ← 按用户过滤
    )
    if status:
        stmt = stmt.where(LearningPath.status == status)
    result = await db.execute(stmt)
    return {"code": 0, "data": result.scalars().all(), "message": "ok"}


@router.post("")
async def create_path(
    req: CreatePathRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),  # ← 新增
):
    path = LearningPath(**req.model_dump(), owner_id=current_user["user_id"])  # ← 绑定所有者
    db.add(path)
    await db.commit()
    await db.refresh(path)
    return {"code": 0, "data": path, "message": "ok"}


@router.patch("/{path_id}")
async def update_path(
    path_id: uuid.UUID,
    req: UpdatePathRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),  # ← 新增
):
    path = await db.get(LearningPath, path_id)
    if not path or path.deleted_at is not None:
        raise HTTPException(status_code=404, detail="路径不存在")
    if path.owner_id != current_user["user_id"]:  # ← 所有权校验
        raise HTTPException(status_code=403, detail="无权访问此路径")
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(path, field, value)
    await db.commit()
    await db.refresh(path)
    return {"code": 0, "data": path, "message": "ok"}
```

**③ 新增删除端点（如果需要）**：
```python
@router.delete("/{path_id}")
async def delete_path(
    path_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    path = await db.get(LearningPath, path_id)
    if not path or path.deleted_at is not None:
        raise HTTPException(status_code=404, detail="路径不存在")
    if path.owner_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权删除此路径")
    path.deleted_at = datetime.now(UTC)
    await db.commit()
    return {"code": 0, "data": None, "message": "ok"}
```

---

### Step 3.2 — 主题（topics.py）

**文件**：`api/routers/topics.py`

**关键：所有操作需要校验父级 LearningPath 的归属**。

```python
from api.auth import get_current_user  # 使用抽取后的公共模块


@router.get("/paths/{path_id}/topics")
async def list_topics(
    path_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # 先校验父路径归属
    path = await db.get(LearningPath, path_id)
    if not path or path.deleted_at is not None:
        raise HTTPException(status_code=404, detail="路径不存在")
    if path.owner_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权访问此路径")

    stmt = select(Topic).where(Topic.path_id == path_id).order_by(Topic.order_index)
    result = await db.execute(stmt)
    return {"code": 0, "data": result.scalars().all(), "message": "ok"}


@router.post("/topics")
async def create_topic(
    req: CreateTopicRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # 校验父路径归属
    path = await db.get(LearningPath, req.path_id)
    if not path or path.deleted_at is not None:
        raise HTTPException(status_code=404, detail="路径不存在")
    if path.owner_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权在此路径下创建主题")

    topic = Topic(**req.model_dump())
    db.add(topic)
    await db.commit()
    await db.refresh(topic)
    return {"code": 0, "data": topic, "message": "ok"}
```

**需要新增导入**：
```python
from api.models import LearningPath  # 用于父级校验
from fastapi import HTTPException
```

---

### Step 3.3 — 任务（tasks.py）

**文件**：`api/routers/tasks.py`

**关键：需要沿 Topic → LearningPath 链路校验归属**。

```python
from api.auth import get_current_user
from api.models import LearningPath, Topic


async def _verify_task_ownership(
    task_id: uuid.UUID, user_id: str, db: AsyncSession
) -> Task:
    """校验任务归属，返回 Task 或抛出 HTTPException。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    # 沿外键链路上溯校验
    topic = await db.get(Topic, task.topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="任务所属主题不存在")
    path = await db.get(LearningPath, topic.path_id)
    if not path or path.deleted_at is not None:
        raise HTTPException(status_code=404, detail="任务所属路径不存在")
    if path.owner_id != user_id:
        raise HTTPException(status_code=403, detail="无权访问此任务")
    return task


@router.get("/topics/{topic_id}/tasks")
async def list_tasks(
    topic_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # 校验 topic 的父级归属
    topic = await db.get(Topic, topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="主题不存在")
    path = await db.get(LearningPath, topic.path_id)
    if not path or path.deleted_at is not None:
        raise HTTPException(status_code=404, detail="主题所属路径不存在")
    if path.owner_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权访问此主题")

    stmt = select(Task).where(Task.topic_id == topic_id).order_by(Task.created_at)
    result = await db.execute(stmt)
    return {"code": 0, "data": result.scalars().all(), "message": "ok"}


@router.post("/tasks")
async def create_task(
    req: CreateTaskRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # 校验 topic 归属
    topic = await db.get(Topic, req.topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="主题不存在")
    path = await db.get(LearningPath, topic.path_id)
    if not path or path.deleted_at is not None:
        raise HTTPException(status_code=404, detail="主题所属路径不存在")
    if path.owner_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权在此主题下创建任务")

    task = Task(**req.model_dump())
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return {"code": 0, "data": task, "message": "ok"}


@router.patch("/tasks/{task_id}/status")
async def update_task_status(
    task_id: uuid.UUID,
    req: UpdateStatusRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    task = await _verify_task_ownership(task_id, current_user["user_id"], db)

    allowed = Task.VALID_TRANSITIONS.get(task.status, set())
    if req.status not in allowed:
        raise HTTPException(
            status_code=400, detail=f"不能从 {task.status} 跳转到 {req.status}"
        )

    task.status = req.status
    task.completed_at = datetime.now(UTC) if req.status == "done" else None
    await db.commit()
    await db.refresh(task)
    return {"code": 0, "data": task, "message": "ok"}
```

---

### Step 3.4 — 抽取认证模块（避免循环导入）

**新建文件**：`api/auth.py`

将 `get_current_user`、`bearer` 从 `server.py` 移到此文件：

```python
"""JWT 认证依赖。"""
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import InvalidTokenError
from typing import Annotated

from api.config import settings

bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> dict[str, str]:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要登录",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = jwt.decode(
            credentials.credentials,
            key=settings.jwt_verifying_key,
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
    except InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录凭证无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise HTTPException(status_code=401, detail="登录凭证缺少有效用户标识")
    return {"user_id": subject}
```

**同时更新 `server.py`**：
```python
# 替换原来的 get_current_user 定义
from api.auth import get_current_user
```

---

## Phase 4：用户系统

### Step 4.1 — 用户模型

**文件**：`api/models.py`

**新增 User 模型**：
```python
from sqlalchemy import Boolean


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=gen_uuid
    )
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String, unique=True)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

---

### Step 4.2 — 密码哈希

**新建文件**：`api/security.py`

```python
"""密码哈希与令牌签发。"""
from datetime import UTC, datetime, timedelta

import jwt
from passlib.context import CryptContext

from api.config import settings

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: str) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": user_id,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_token_expire_minutes),
    }
    return jwt.encode(claims, settings.jwt_signing_key, algorithm="HS256")
```

**添加依赖到 `requirements.txt`**：
```
passlib[argon2]==1.7.4
```

---

### Step 4.3 — 认证路由

**新建文件**：`api/routers/auth.py`

```python
"""注册、登录、令牌刷新。"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from api.db import get_db
from api.models import User
from api.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str
    password: str
    email: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # 秒


class UserProfile(BaseModel):
    id: str
    username: str
    email: str | None


@router.post("/register", response_model=TokenResponse)
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    # 检查用户名是否已存在
    stmt = select(User).where(User.username == req.username)
    existing = await db.execute(stmt)
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="用户名已存在")

    user = User(
        username=req.username,
        email=req.email,
        hashed_password=hash_password(req.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token(str(user.id))
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_access_token_expire_minutes * 60,
    )


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    stmt = select(User).where(User.username == req.username)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    # 统一错误信息，不泄露用户是否存在
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被禁用")

    token = create_access_token(str(user.id))
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_access_token_expire_minutes * 60,
    )


@router.get("/me", response_model=UserProfile)
async def get_profile(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User).where(User.id == current_user["user_id"])
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return UserProfile(
        id=str(user.id), username=user.username, email=user.email
    )
```

**注册路由到 `server.py`**：
```python
from api.routers import auth
app.include_router(auth.router)
```

---

## Phase 5：前端集成

### Step 5.1 — 更新 API 客户端

**文件**：`/Users/zhangweiwei/Desktop/code/web/my-app/src/lib/api.ts`

**修改点**：

```typescript
// 1. streamChat 签名：移除 userId 参数，token 从外部传入
export async function streamChat(
  message: string,
  sessionId: string,
  accessToken: string,          // ← 替换 userId
  onToken: (token: string) => void,
  signal?: AbortSignal,
  onUsage?: (usage: TokenUsage) => void
): Promise<string> {
  // ...
  response = await fetch(`${API_URL}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      Authorization: `Bearer ${accessToken}`,  // ← 新增
    },
    body: JSON.stringify({
      message,
      session_id: sessionId,
      // user_id 已从请求体移除
    }),
    signal,
  });
  // ...
}
```

```typescript
// 2. 新增认证 API
export async function login(
  username: string,
  password: string
): Promise<{ access_token: string; expires_in: number }> {
  const response = await fetch(`${API_URL}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(extractErrorMessage(payload) || "登录失败");
  }
  return response.json();
}

export async function register(
  username: string,
  password: string,
  email?: string
): Promise<{ access_token: string; expires_in: number }> {
  const response = await fetch(`${API_URL}/api/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, email }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(extractErrorMessage(payload) || "注册失败");
  }
  return response.json();
}

export async function getProfile(accessToken: string) {
  const response = await fetch(`${API_URL}/api/auth/me`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) throw new Error("获取用户信息失败");
  return response.json();
}
```

---

### Step 5.2 — 更新 App.tsx

**文件**：`/Users/zhangweiwei/Desktop/code/web/my-app/src/App.tsx`

**修改点**：

```typescript
// 1. 移除 localStorage user_id 逻辑，替换为 token 状态
// 删除：
// localStorage.setItem("user_id", localStorage.getItem("user_id") || makeId());

// 2. 新增状态
const [accessToken, setAccessToken] = useState<string | null>(
  () => localStorage.getItem("access_token")
);
const [username, setUsername] = useState<string | null>(null);

// 3. 在 runRequest 中使用 accessToken 替代 user_id
const reply = await streamChat(
  text,
  sessionId,
  accessToken!,  // ← 替换 user_id
  (token) => { /* ... */ },
  controller.signal,
  (usage) => { /* ... */ }
);

// 4. 401 处理：token 过期时清除状态，跳转登录
// 在 runRequest 的 catch 中添加：
if (error.message.includes("401") || error.message.includes("登录")) {
  localStorage.removeItem("access_token");
  setAccessToken(null);
  // 显示登录界面
}
```

**注意**：前端改动较大，需要增加登录/注册表单组件。建议新建 `LoginForm.tsx` 组件，在 `accessToken` 为 null 时渲染登录界面，否则渲染聊天界面。

---

## Phase 6：自动回归测试

### Step 6.1 — JWT 验证器测试

**新建文件**：`tests/test_jwt_auth.py`

```python
"""JWT 认证离线回归测试。"""
import time
import unittest
from datetime import UTC, datetime, timedelta

import jwt


SECRET = "test-secret-key-for-offline-regression"
ISSUER = "test-issuer"
AUDIENCE = "test-audience"


def make_token(
    sub: str = "user-123",
    iss: str = ISSUER,
    aud: str = AUDIENCE,
    exp_offset: int = 900,
    key: str = SECRET,
    algorithm: str = "HS256",
) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": sub,
        "iss": iss,
        "aud": aud,
        "iat": now,
        "exp": now + timedelta(seconds=exp_offset),
    }
    return jwt.encode(claims, key, algorithm=algorithm)


class JWTValidationTests(unittest.TestCase):
    """模拟 get_current_user 的验证逻辑（不依赖 FastAPI）。"""

    def _validate(self, token: str) -> dict:
        """从 api/auth.py 提取的验证逻辑副本。"""
        claims = jwt.decode(
            token,
            key=SECRET,
            algorithms=["HS256"],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise ValueError("缺少有效 sub")
        return {"user_id": subject}

    def test_valid_token_returns_user_id(self):
        token = make_token(sub="user-456")
        result = self._validate(token)
        self.assertEqual(result["user_id"], "user-456")

    def test_expired_token_rejected(self):
        token = make_token(exp_offset=-60)  # 1 分钟前过期
        with self.assertRaises(jwt.ExpiredSignatureError):
            self._validate(token)

    def test_wrong_signing_key_rejected(self):
        token = make_token(key="wrong-key")
        with self.assertRaises(jwt.InvalidSignatureError):
            self._validate(token)

    def test_wrong_issuer_rejected(self):
        token = make_token(iss="wrong-issuer")
        with self.assertRaises(jwt.InvalidIssuerError):
            self._validate(token)

    def test_wrong_audience_rejected(self):
        token = make_token(aud="wrong-audience")
        with self.assertRaises(jwt.InvalidAudienceError):
            self._validate(token)

    def test_missing_sub_rejected(self):
        now = datetime.now(UTC)
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + timedelta(seconds=900),
            # 故意不含 sub
        }
        token = jwt.encode(claims, SECRET, algorithm="HS256")
        with self.assertRaises(jwt.MissingRequiredClaimError):
            self._validate(token)

    def test_empty_sub_rejected(self):
        token = make_token(sub="")
        with self.assertRaises(ValueError):
            self._validate(token)


if __name__ == "__main__":
    unittest.main()
```

**运行**：
```bash
cd /Users/zhangweiwei/Desktop/code/agent
python -m unittest tests.test_jwt_auth -v
```

---

### Step 6.2 — 资源隔离测试（需要数据库）

**新建文件**：`tests/test_resource_isolation.py`

```python
"""跨用户资源隔离测试（需要真实数据库或 mock）。"""
import unittest
import uuid


class ResourceIsolationTests(unittest.TestCase):
    """
    验证用户 A 无法访问用户 B 的资源。
    需要数据库连接，跳过条件由 CI 环境变量控制。
    """

    def test_chat_request_without_user_id_accepted(self):
        """ChatRequest 不再接受 user_id 字段。"""
        from api.server import ChatRequest

        # 不包含 user_id 的请求应该正常创建
        req = ChatRequest(message="test", session_id="s1")
        self.assertEqual(req.message, "test")
        self.assertFalse(hasattr(req, "user_id") and req.user_id)

    def test_path_ownership_filter(self):
        """list_paths 应按 owner_id 过滤。"""
        # 此测试需要数据库 fixture，标记为集成测试
        self.skipTest("需要数据库集成环境")

    def test_cannot_access_other_users_path(self):
        """用户 A 访问用户 B 的 LearningPath 应返回 403。"""
        self.skipTest("需要数据库集成环境")

    def test_cannot_access_other_users_topic(self):
        """通过 path_id 访问其他用户的 Topic 应返回 403。"""
        self.skipTest("需要数据库集成环境")

    def test_cannot_access_other_users_task(self):
        """通过 topic_id 访问其他用户的 Task 应返回 403。"""
        self.skipTest("需要数据库集成环境")


if __name__ == "__main__":
    unittest.main()
```

---

## Phase 7：文档更新

### Step 7.1 — 核对 jwt_auth.md 的实际状态

**文件**：`docs/jwt_auth.md`

当前后端已有登录、令牌签发与验证及资源归属代码。[jwt_auth.md](jwt_auth.md) 继续区分已实现能力与待验收范围。后续每次认证代码变动，应同步核对该文的功能与验收边界。

---

### Step 7.2 — 更新 TODO.md

在 TODO 中标记 JWT 集成各阶段的完成状态。

---

## 验收清单（最终）

以下勾选表示**已通过验收**，不表示本地代码中完全没有对应实现；尚无验证记录的项目暂不勾选：

- [ ] `requirements.txt` 包含 `PyJWT` 和 `passlib[argon2]`
- [ ] `Settings` 模型包含全部 JWT 配置字段，启动时空配置会快速失败
- [ ] `ChatRequest` 不包含 `user_id` 字段
- [ ] `/chat` 的锁、checkpoint、ChatLog 全部使用 `current_user["user_id"]`
- [ ] `api/auth.py` 存在，`get_current_user` 可被所有路由导入（无循环导入）
- [ ] `paths.py`：创建时绑定 `owner_id`，列表按 `owner_id` 过滤，更新时校验所有权
- [ ] `topics.py`：所有端点校验父级 LearningPath 归属
- [ ] `tasks.py`：所有端点沿 Topic → LearningPath 链路校验归属
- [ ] 用户表存在，注册/登录接口可用
- [ ] 密码使用 Argon2 哈希存储
- [ ] 前端 `streamChat` 使用 `Authorization: Bearer` header
- [ ] 前端不再从 `localStorage` 读取 `user_id` 作为身份
- [ ] 新增的 JWT 与资源隔离离线测试全部通过（当前尚无 `tests/test_jwt_auth.py`）
- [ ] `docs/jwt_auth.md` 描述与代码状态一致
- [ ] 两个不同 token 的用户互相无法访问对方的 path/topic/task
- [ ] 伪造请求体中的 `user_id` 字段不影响实际身份

---

## 实施顺序总结

```
Phase 1 (基础设施)     → 依赖 + 配置 + 启动校验
Phase 2 (核心修复)     → ChatRequest 移除 user_id，统一认证身份
Phase 3 (路由保护)     → paths/topics/tasks 认证 + 所有权校验
Phase 4 (用户系统)     → User 模型 + 密码哈希 + 注册/登录
Phase 5 (前端集成)     → Authorization header + 登录状态管理
Phase 6 (测试)         → JWT 验证测试 + 资源隔离测试
Phase 7 (文档)         → 更新 jwt_auth.md + TODO
```

每个 Phase 可以独立提交一个 commit，便于回滚和审查。
