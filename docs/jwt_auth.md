# JWT 学习与本项目集成指南

本文先介绍 JWT 的组成和验证方式，再记录项目的本地实现与剩余验收。当前代码已有用户表、注册/登录、JWT 签发与验证，以及 `/chat`、路径、主题、任务接口的认证/所有权代码。前端接入、双用户端到端隔离及生产密钥管理仍未验收，因此不能据此认定服务已可对外部署。

## JWT 是什么

JWT（JSON Web Token）是一种承载声明（claims）的令牌格式。常见的签名 JWT 由三段组成：

```text
header.payload.signature
```

- Header 描述令牌类型和签名算法，例如 `{"typ":"JWT","alg":"HS256"}`。
- Payload 保存声明，例如用户 ID 和令牌有效期。
- Signature 根据 Header、Payload 和密钥计算，用来发现令牌被篡改。

这些部分使用 Base64URL 编码。**签名 JWT 默认没有加密**：拿到令牌的人可以解码并读取 Payload。因此不要在 Payload 放密码、API key 或其他秘密。JWT 的签名用于验证完整性和签发方；它本身不负责登录、用户权限或数据归属校验。

常见标准声明：

| 声明 | 用途 | 本项目建议 |
| --- | --- | --- |
| `sub` | 令牌代表的主体 | 使用数据库中的稳定用户 ID，作为可信 `user_id` |
| `iss` | 签发者 | 配置固定的服务标识 |
| `aud` | 预期接收方 | 配置本 API 的固定标识 |
| `iat` | 签发时间 | 便于审计和生命周期管理 |
| `exp` | 到期时间 | API 必须拒绝已过期令牌 |
| `nbf` | 开始生效时间 | 仅在确有需要时设置 |

`sub`、`iss`、`aud`、`exp` 等声明的定义见 [RFC 7519](https://www.rfc-editor.org/rfc/rfc7519/)；JWT 声明可以是可选的，但应用可以要求关键声明必须存在。

## 签名和验证

HS256 使用同一个共享密钥签发和验证，适合一个受控的后端服务；能读取该密钥的服务也能签发令牌。RS256 使用私钥签发、公钥验证，适合多个 API 服务只需验证令牌、而不应持有签发能力的场景。个人学习项目可以先选 HS256，但密钥必须来自安全随机生成器、只保存在环境变量或密钥管理服务中，不能提交到 Git。

验证端必须固定允许的算法，并验证签名、有效期和应用所依赖的 `iss`、`aud`、`sub`。不能只解码 Payload，也不能让令牌 Header 自己决定验证算法。可以查看 [PyJWT 官方用法](https://pyjwt.readthedocs.io/en/stable/usage.html) 和 [OWASP JWT 安全建议](https://cheatsheetseries.owasp.org/cheatsheets/JSON_Web_Token_Cheat_Sheet.html)。

JWT 通常作为 Bearer Token 通过 HTTPS 请求头发送：

```http
Authorization: Bearer <access_token>
```

访问令牌应有较短有效期。若需要长时间登录，应设计刷新令牌、轮换和撤销流程；不要把“签名正确”误当成“用户仍然有权访问任意资源”。JWT 负责认证令牌代表谁，资源授权仍要由服务端按用户过滤和检查。

## 当前项目的身份问题

原版 [api/server.py](../api/server.py) 的 `ChatRequest` 要求客户端提交 `user_id`，该值参与 checkpoint 线程 ID、锁 ID、聊天日志和记忆逻辑。当前代码已移除这一请求字段，改用 JWT `sub`；前端调用是否同步更新尚未在此仓库验证。复合线程 ID 本身仍只是状态隔离键，不能替代身份认证和资源授权。

当前 [paths.py](../api/routers/paths.py)、[topics.py](../api/routers/topics.py)、[tasks.py](../api/routers/tasks.py) 已加入认证依赖，并通过 `LearningPath.owner_id` 检查父级归属；完整路由覆盖和跨用户访问仍需自动化与端到端验证。

当前代码已选择自建登录，加入 `User` 表、Argon2 密码哈希、`/api/auth/register`、`/api/auth/login` 和 `/api/auth/me`。生产部署仍需决定是否继续维护自建身份，或迁移到 OIDC：

1. **推荐上线方案：**接入托管或自建 OIDC 身份提供方，由它完成登录和令牌签发，FastAPI 校验其签名及 `iss`、`aud`，并将稳定的 `sub` 映射为项目用户 ID。
2. **学习用自建方案：**增加用户表和注册/登录流程，安全地哈希并验证密码，再由服务端签发短期访问 JWT。不能把客户端随便传来的 UUID 直接签成登录令牌。

## 集成到项目的目标结构

认证后，每个受保护请求按以下顺序处理：

```text
浏览器登录并取得 access token
        ↓ Authorization: Bearer <token>
FastAPI 验证签名、算法、exp、iss、aud
        ↓
从 token.sub 得到可信 user_id
        ↓
按该 user_id 访问 checkpoint、ChatLog、长期记忆和学习路径
```

### 1. 增加令牌配置和依赖

当前 `requirements.txt` 已加入 `PyJWT` 与 `passlib[argon2]`；[config.py](../api/config.py) 配置签名/验证密钥、issuer、audience 和有效期，[security.py](../api/security.py) 签发 HS256 令牌，[auth.py](../api/auth.py) 验证固定算法及必要声明。以下代码用于解释签发与验证边界，实际行为以这些文件为准。开发环境 `.env` 中的密钥不得提交。

自签发模式的令牌只能在用户凭证已被验证、并且用户账号可用之后创建。签发函数可以保持很小：

```python
from datetime import UTC, datetime, timedelta

import jwt


def create_access_token(user_id: str) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": user_id,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=15),
    }
    return jwt.encode(claims, JWT_SIGNING_KEY, algorithm="HS256")
```

登录路由应先按用户名或邮箱查用户，再用 Argon2id 等密码哈希库验证存储的密码哈希；验证失败返回统一的 401，验证成功后才调用 `create_access_token(str(user.id))`。绝不能在登录路由中接受调用者提供的 `user_id` 并直接为它签发令牌。上面的 15 分钟只是短期 access token 的示例时长，需要和刷新/重新登录策略一并设计。

以下是**验证依赖的示意代码**，需结合所选签发方和项目配置实现；不是可直接复制后即有登录能力的完整认证系统：

```python
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import InvalidTokenError

bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer)
    ],
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
            key=JWT_VERIFYING_KEY,
            algorithms=["HS256"],  # 固定在服务端配置，不读取 token header 来决定
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
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

把 `JWT_VERIFYING_KEY`、`JWT_ISSUER`、`JWT_AUDIENCE` 换成项目 `Settings` 中的配置；示例中的 HS256 只适用于共享密钥部署。公钥验证时应从可信 OIDC 配置/JWKS 获取公钥，并将允许算法设为签发方明确规定的算法。不要接受任意 issuer，也不要只验证签名却不验证 audience。

### 2. 删除聊天请求里的 `user_id`

把 `ChatRequest` 改为只接收用户可控制的内容，例如 `message` 和 `session_id`；将认证用户注入 endpoint：

```python
async def chat_api(
    request: ChatRequest,
    http_request: Request,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = current_user["user_id"]
    lock_id = checkpoint_lock_id(user_id, request.session_id)
    # chat_stream(..., user_id=user_id, ...)
    # ChatLog(user_id=user_id, ...)
```

前端可以继续保存非敏感的 `session_id`，但不再创建、发送或信任 `user_id`。所有受保护请求（包括 SSE 的 `fetch`）添加 `Authorization` 请求头。对于 Bearer Header，浏览器 `fetch` 可以显式发送此 Header；若改用 HttpOnly Cookie，则需要额外设计 CSRF 防护和跨域 Cookie 配置。

### 3. 把认证贯穿所有数据访问

- **Checkpoint：**线程 ID 和 advisory lock ID 继续使用认证所得的 `user_id` 与请求的 `session_id` 组合。
- **ChatLog：**查询、读取历史和写入都使用认证所得 `user_id`；当前写接口也不得接纳客户端指定的所有者。
- **长期记忆：**查询、插入、替换和删除使用服务端绑定的用户身份，不能让模型参数或请求体覆盖该值。
- **学习路径：**创建路径时设置 `owner_id=user_id`；列表按 `owner_id` 过滤；通过 path/topic/task ID 访问下级资源时，沿父级关系确认该资源属于当前用户。`topics` 和 `tasks` 的 create/read/update 端点都要做这项检查。
- **其他 API：**除公开的健康检查外，盘点每个路由；默认要求认证，只有明确设计为公开的接口才豁免。

当前代码也修改了路径、主题和任务接口，但尚未完成逐路由自动回归与真实双用户验收；长期记忆工具和独立 Graph 示例的可信身份绑定仍需单独检查。

### 4. 前端登录与 SSE 请求

登录成功后，前端得到 access token；聊天请求保持 POST SSE，不改成原生 `EventSource`：

```ts
await fetch(`${API_URL}/chat`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": `Bearer ${accessToken}`,
  },
  body: JSON.stringify({ message, session_id: sessionId }),
});
```

不要将长期有效的 bearer token 放在 URL、日志或错误消息中。浏览器存储方式要和威胁模型相符：内存存储会在刷新后丢失；`localStorage` 可被同源 JavaScript 读取，XSS 会增加令牌被窃取的风险；若使用 HttpOnly Cookie，必须处理 CSRF。先确定是否需要刷新后保持登录，再选择 token 生命周期与存储方式。

## 剩余实施与验收顺序

1. **核对本地实现。**审查注册/登录、JWT 验证、`/chat` 和每个资源路由的所有权判断；确认已有代码与配置实际可运行。
2. **完成前端接入。**增加登录状态和 token 生命周期处理；移除 `localStorage.user_id` 作为身份来源；为普通 API 与 SSE fetch 附加 Authorization Header。
3. **补齐自动回归。**测试缺 token、格式错误、签名错误、过期 token、错误 issuer/audience 均返回 401；有效 token 能访问自己的数据；用户 A 无法读取或修改用户 B 的 checkpoint、记忆、路径、主题和任务。
4. **端到端验收。**用两个用户分别登录，验证聊天历史、SSE、停止和重试、长期记忆以及学习路径的隔离；确认刷新后会话恢复行为符合 token 生命周期设计。
5. **确定部署身份方案。**若继续自建登录，补充生产密钥托管、轮换和撤销；若改用 OIDC，重新验证签名算法、issuer、audience 与稳定用户 ID 映射。

建议认证失败使用 HTTP 401；令牌有效但没有资源权限时，按接口的资源隐藏策略返回 403 或 404。SSE 连接在响应开始后不能再更改 HTTP 状态，因此要在创建 `StreamingResponse` 前完成认证和请求授权。

## 验收清单

- [ ] 没有 Authorization Header 的受保护 API 返回 401。
- [ ] 无效签名、过期、错误 `iss` / `aud`、缺少 `sub` 均被拒绝。
- [ ] JWT 的算法由服务端固定；密钥不在仓库和日志中。
- [ ] `/chat` 不再接受客户端身份作为可信 `user_id`。
- [ ] 两个用户的 checkpoint、ChatLog、长期记忆和学习路径数据互相隔离。
- [ ] `paths`、`topics`、`tasks` 每个读写操作均检查所有权。
- [ ] 前端在普通请求和 SSE 请求中携带认证信息；过期后能清晰处理 401。
- [ ] 认证测试不依赖真实 LLM 或 Milvus。

## 参考资料

- [RFC 7519：JSON Web Token](https://www.rfc-editor.org/rfc/rfc7519/)
- [FastAPI：OAuth2 与 JWT 教程](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/)
- [PyJWT：使用与声明验证](https://pyjwt.readthedocs.io/en/stable/usage.html)
- [OWASP：JSON Web Token 安全建议](https://cheatsheetseries.owasp.org/cheatsheets/JSON_Web_Token_Cheat_Sheet.html)
