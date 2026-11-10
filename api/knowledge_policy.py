# 模块职责：默认允许登录用户共享编辑；管理员模式只信任服务端 UUID 名单，不信任客户端自报角色。
"""Shared editing remains default; optional explicit administrator allowlist."""
# 导入 os，用于读取环境变量并执行必要的文件系统操作。
import os
# 导入 uuid，用于验证 UUID 或生成稳定/随机标识。
import uuid
# 从 fastapi 导入 HTTPException，供下方相关数据结构与业务步骤。
from fastapi import HTTPException


# 定义函数：验证用户 UUID 与服务端管理策略；shared 放行，admin 检查管理员名单。
def require_knowledge_manager(current_user):
    # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
    try:
        # 准备「通过校验的用户 UUID」：校验/构造 UUID。
        actor = uuid.UUID(current_user['user_id'])
    # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
    except (ValueError, KeyError, TypeError) as exc:
        # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
        raise HTTPException(401, '用户标识无效') from exc
    # 准备「当前检索/权限策略模式」：读取环境配置（未设置时采用代码中的默认值）。
    mode = os.getenv('KNOWLEDGE_MANAGEMENT_MODE', 'shared')
    # 未知权限模式按配置错误拒绝，不能意外退化成所有用户可管理。
    if mode not in {'shared', 'admin'}:
        # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
        raise HTTPException(503, '知识库管理策略配置无效')
    # 只有管理员模式才检查服务端名单；shared 模式保持原来的登录用户共享编辑策略。
    if mode == 'admin':
        # 读取服务端配置的管理员 UUID 名单，去除空项；不读取请求中的自报角色。
        allowed = {v.strip() for v in os.getenv('KNOWLEDGE_ADMIN_IDS', '').split(',') if v.strip()}
        # 当前用户不在管理员名单时拒绝修改操作，即使客户端声称自己是管理员也不放行。
        if str(actor) not in allowed:
            # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
            raise HTTPException(403, '仅知识库管理员可执行此操作')
    # 返回已通过当前管理策略校验的用户 UUID。
    return actor
