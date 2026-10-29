# 会话与消息表设计（V0.1）

**历史设计稿。** 当前已实施的表、接口和执行事件见 [当前会话数据结构](session_current.md)。本文保留 2026-10-23 的设计背景；后续的未实施判断、建议分页数字和首版 DDL 不代表当前代码。

初稿日期：2026-10-23；当前状态核对：2026-10-29。会话创建、本人历史列表、消息分页、前端刷新恢复、记忆结果和执行事件已有代码；真实体验由用户按需验收。旧数据导入仍未实施。实际表定义以 [ORM](../api/models.py) 和 [迁移文件](../migrations/) 为准；下方首版 DDL 保留设计背景，**不含后来增加的 `memory_status` 与 `chat_run_events`，不要在现有库重复执行**。

当前增量：`chat_messages.memory_status` 记录记忆处理结果；`chat_run_events` 按 assistant message、sequence 保存阶段、状态、摘要、来源与时间。对应迁移分别为 [记忆状态](../migrations/20261027_chat_memory_status.sql) 和 [执行事件](../migrations/20261027_chat_run_events.sql)。`GET /api/sessions/{id}/messages` 在校验会话归属后批量带回事件。前端初次取最近 100 条，旧消息按需加载。

## 范围和关系

users 1 → N chat_sessions 1 → N chat_messages。

- users：沿用现有用户表，主键 UUID。
- chat_sessions：新增，保存归属、标题、排序和删除状态。
- chat_messages：新增，保存面向用户的消息及生成状态；V0.1 一次请求一条 user 和一条 assistant，不存 system/tool 消息。
- LangGraph checkpoint：沿用，保存模型实际上下文、工具调用和执行状态；不改其框架内部表结构。
- chat_logs / chat_feedback：保留。已有反馈关联 chat_logs.id，暂不修改其外键与既有接口。完成/停止时继续返回 chat_log_id。
- Milvus：长期记忆按用户保存；删除会话不自动删除长期记忆。

消息表是 UI 展示记录，checkpoint 是模型执行上下文，两者不是相互替代关系。不得仅用 UI 消息重建上下文，以免丢失工具消息。

## 首版 DDL（历史设计，当前表还包含后续迁移）

ID 由应用生成，避免引入数据库 UUID 扩展依赖。全部业务时间使用 timestamptz，存储绝对时间、展示时转换为用户时区。

```sql
CREATE TABLE chat_sessions (
    id                   uuid PRIMARY KEY,
    owner_id             uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    title                varchar(200) NOT NULL DEFAULT '新会话',
    checkpoint_thread_id text NOT NULL UNIQUE,
    legacy_session_id    text,
    next_message_seq     bigint NOT NULL DEFAULT 1 CHECK (next_message_seq >= 1),
    last_message_at      timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    deleted_at           timestamptz,
    CHECK (length(btrim(title)) > 0),
    UNIQUE (owner_id, legacy_session_id)
);

-- 列表仅展示未删除、已有消息的会话。
CREATE INDEX ix_chat_sessions_owner_activity
    ON chat_sessions(owner_id, last_message_at DESC, id DESC)
    WHERE deleted_at IS NULL AND last_message_at IS NOT NULL;

CREATE TABLE chat_messages (
    id             uuid PRIMARY KEY,
    session_id     uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE RESTRICT,
    request_id     uuid NOT NULL,
    seq            bigint NOT NULL CHECK (seq >= 1),
    role           varchar(16) NOT NULL CHECK (role IN ('user', 'assistant')),
    content        text NOT NULL DEFAULT '',
    status         varchar(16) NOT NULL CHECK (
        status IN ('streaming', 'completed', 'stopped', 'failed', 'interrupted')
    ),
    error_code     varchar(64),
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    UNIQUE (session_id, seq),
    UNIQUE (session_id, request_id, role),
    CHECK (role <> 'user' OR status = 'completed'),
    CHECK (
        (status = 'streaming' AND finished_at IS NULL)
        OR (status <> 'streaming' AND finished_at IS NOT NULL)
    )
);
```

`UNIQUE(session_id, seq)` 自带支持按会话和序号读取的索引，不重复创建。V0.1 暂不加向量列、附件表、全文索引、工具执行表或生成分支表。

## 字段与产品行为

### chat_sessions

- id：新会话由服务端生成。前端保存在 URL，刷新后带同一个 ID 请求；不能每次发送消息都生成会话 ID。
- owner_id：从认证信息取得，不采信客户端提交的归属。
- title：首条消息截取作为初始标题，允许用户重命名；首版不额外调用模型生成标题。
- checkpoint_thread_id：保存实际执行线程。新会话按现有 make_thread_id(user_id, session_id) 生成后持久化；后续只使用已授权会话记录中的该值。
- legacy_session_id：迁移旧 ChatLog.session_id 时保留原始字符串，仅作兼容映射；新会话为 NULL。
- next_message_seq：每次预留两个序号后加二，保证用户消息与助手消息顺序稳定。序号可以有空洞。
- last_message_at：首条用户消息被接受时更新，完成助手消息时再更新；不随每个 token 更新。用它排序，重命名不使旧会话跳到顶部。
- updated_at：业务记录更新时间，应用每次修改显式维护；DEFAULT now() 只管创建，不自动追踪更新。
- deleted_at：软删除隐藏列表，禁止继续发送。首版无物理清理入口。

### chat_messages

- id：独立消息标识；request_id：一次问答请求的标识，两条消息共享它。
- seq：会话内稳定排序依据，不能仅依赖时间戳。
- role：V0.1 只面向用户和助手。工具细节保留在 checkpoint，未来需要展示再扩展事件模型。
- content：保存正文；assistant 可先为空，定期保存部分回复，终态保存最终结果。不把“生成失败”等系统提示伪装成正文，不把停止标记拼接进内容。
- status：streaming 为生成中；completed 为完成；stopped 为用户停止；failed 为已知失败；interrupted 为异常退出、结果不能确认。user 消息始终 completed，assistant 状态反映本轮执行。
- error_code：面向客户端的稳定错误码，不保存供应商原始异常、密钥或完整堆栈。
- finished_at：终态发生时间。不能以“消息存在”推断生成已完成。

状态：assistant streaming → completed / stopped / failed / interrupted。终态不自动改回 streaming；显式重试使用新的 request_id。V0.1 不提供重新生成后的回答分支。

## 写入与并发约定

1. 点击新建先打开空白页；第一次发送时幂等创建会话，创建失败不得启动模型。可用前端稳定的建会话请求键协调重试；无消息的空会话不出现在列表。
2. 验证 JWT 用户拥有且未删除该会话，再获取现有“用户/会话”执行锁；新旧线程标识必须使用一致的映射，锁覆盖完整问答生命周期。
3. 短事务锁定会话行，检查 request_id：已有 user 行时比对原文，内容不同返回冲突；相同且生成中返回进行中状态；相同且终态返回已保存结果。不得再次调用模型。明确“重试生成”使用新 request_id。
4. 同一短事务分配 seq、插入 user(completed) 和 assistant(streaming)，更新会话排序时间并提交，然后才调用模型。
5. SSE 发送 token；部分正文按节流策略另用短事务保存，不逐 token 写库。V0.1 不承诺进程崩溃时最后未保存的部分 token 能恢复。
6. 正常完成或主动停止后，在同一业务事务更新 assistant 正文/终态、会话时间、兼容 ChatLog；成功提交后再发 done/stopped 和 chat_log_id。
7. 模型失败更新 assistant 为 failed；客户端断线的处理必须区分主动停止和未知异常。进程异常后，确认没有仍在执行的工作再将遗留 streaming 标为 interrupted，不能凭一个固定超时时间盲目重跑。

重要：现有 server.py 持有 transaction advisory lock 到 ChatLog 提交。上述方案需要先提交消息占位再流式生成，因此不能直接沿用该事务锁：第一次提交会释放它。实现时选择专用连接持有 session advisory lock，并在 finally 解锁/归还连接；业务写入另用短事务。该变化需测试多 worker、取消、断线和连接泄漏；锁策略没有验证前不能上线。

checkpoint 与展示消息仍可能使用不同事务，无法声称原子一致。需要记录可关联的请求/消息 ID；发现 checkpoint 已完成但消息仍 streaming 时，先核对结果并修复，不直接重复调用模型。反之，展示记录完成但对应上下文不可用时提示不可继续或进入修复流程。V0.1 不承诺断线续传，只承诺重新打开后显示已保存的真实状态。

## 查询与权限

- 会话列表：owner_id = 当前用户、deleted_at IS NULL、last_message_at IS NOT NULL，按 last_message_at DESC, id DESC 游标分页，建议每页 20 条。
- 消息列表：先验证会话归属和未删除，再按 seq 查询；首次取最近 50 条并正序展示，向前加载用 seq 游标。
- 重命名/删除/继续聊天：全部按 id + owner_id + deleted_at 查询，不能只按消息 ID 或客户端会话 ID 操作。
- 删除与发送要共享同一会话执行锁：V0.1 正在生成时拒绝删除并提示先停止，避免删除后任务继续写入。
- 不在 messages 重复存 owner_id；归属统一通过 sessions 验证。外键只保证引用有效，不会自动完成权限校验。
- 软删除不会抹除正文与 checkpoint；正式清理需后续明确联动 ChatLog、反馈外键、checkpoint 的保留策略。

## 旧数据衔接

1. 备份，在隔离库演练，使用版本化迁移新增两表；不依赖 create_all 修改已有表结构。
2. 按 ChatLog 的 (user_id, session_id) 分组。先核对 user_id 能否匹配真实 users.id；未知用户或格式异常进入待处理清单，不强制转换、不归给其他用户。
3. 每组创建一个新 UUID 会话，legacy_session_id 保存旧字符串，checkpoint_thread_id 使用原来的 make_thread_id(旧 user_id, 旧 session_id)。不能拿新 UUID 重新哈希后丢失历史执行线程。
4. 每条 ChatLog 拆为一条 user 和一条 assistant，同一旧记录 ID 可作为迁移 request_id；按 created_at, id 稳定排序分配 seq。finished_at 可暂用旧 created_at，并标注迁移时间精度有限。
5. 旧表没有结构化状态，不能无证据宣称全部 completed。核对明确的停止标记并转换为 stopped；不明确的异常记录人工核查。旧失败请求未写入 ChatLog，无法凭空补出。
6. 迁移不重复导入：用 (session_id, request_id, role) 唯一约束保证重跑安全。checkpoint 中比 ChatLog 更完整的历史须先核对，不盲目覆盖。
7. 分阶段切换历史读取和发送流程，兼容 ChatLog/feedback。部署期间避免两版应用并行使用不同会话映射/锁键；先停写或受控切换。
8. 校验用户归属、条数、顺序、状态和线程续聊，再开放使用。旧 ChatLog 保留，失败可回退读取，不删源数据。

## 实施前验收清单

- 同一会话连续发送、刷新、切换后，消息顺序与 checkpoint 续聊一致。
- 同一 request_id 重发不会重复创建或重复调用模型；同键不同正文明确冲突。
- 两个用户的列表、消息、重命名、删除、继续聊天均隔离。
- streaming、停止、失败与进程中断后重新打开，展示真实状态。
- 生成中删除被拒绝；软删除后不可读取或继续，不影响其他会话和长期记忆。
- 旧数据可重跑迁移，未知用户不误归属，原 checkpoint 线程可继续。
- 兼容 ChatLog 与反馈入口，事务失败不发假 done；锁连接在取消和异常路径下均释放。

## 2026-10-23 数据库实施记录

- 初始情况：chat_sessions 已由界面创建且无记录，chat_messages 不存在。
- 修正 next_message_seq 的非空约束、标题默认值、创建/更新时间默认值，添加会话列表索引；新增消息表及外键、唯一和 CHECK 约束。
- 随机临时 schema 中执行迁移两次，验证可重跑，并完成 14 项默认值、插入、外键、唯一和状态约束检查，随后回滚。
- 实际迁移在单个事务中执行，设置锁等待和语句超时。迁移前结构快照位于被 Git 忽略的 output/migrations/chat-history-before-d70120cf.json，不含连接密钥及用户正文。
- 实际数据库再次通过 14 项行为检查，测试数据全部回滚；合计 28 项检查通过。原有各表行数未变，新会话表和消息表均为 0 条。
- 未导入旧聊天，未修改现有 ORM、聊天接口和反馈接口。用户、聊天日志、反馈和 checkpoint 数据均保留。
- updated_at 仍由未来应用显式维护，UUID 由应用生成。数据库就绪不代表历史会话功能已接通。

## 2026-10-26 后端接入

新增会话/消息 ORM、创建会话与带请求幂等的 /chat；锁连接池与 checkpoint 连接池独立。修改地图、接口调用和验证边界见 [本轮交付说明](session_storage_delivery.md)。本轮未对 public 表执行新迁移或导入旧数据。
