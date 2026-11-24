"""跨 Worker 的 RPM 与并发租约；Redis 脚本在单个主节点原子裁决。"""

import uuid
from dataclasses import dataclass
from typing import Protocol


WINDOW_MS = 60_000

ACQUIRE_SCRIPT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local run_id = ARGV[1]
local rpm_limit = tonumber(ARGV[2])
local concurrent_limit = tonumber(ARGV[3])
local lease_ms = tonumber(ARGV[4])
local window_ms = tonumber(ARGV[5])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window_ms)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
if redis.call('ZSCORE', KEYS[2], run_id) then
    return {0, redis.call('ZCARD', KEYS[1]), redis.call('ZCARD', KEYS[2])}
end
-- 同一 Run 的租约已释放或过期时拒绝重新申请，避免响应丢失引发第二次执行。
if redis.call('ZSCORE', KEYS[1], run_id) then
    return {3, redis.call('ZCARD', KEYS[1]), redis.call('ZCARD', KEYS[2])}
end
local rpm = redis.call('ZCARD', KEYS[1])
local active = redis.call('ZCARD', KEYS[2])
if rpm >= rpm_limit then return {1, rpm, active} end
if active >= concurrent_limit then return {2, rpm, active} end
redis.call('ZADD', KEYS[1], now, run_id)
redis.call('PEXPIRE', KEYS[1], window_ms + 1000)
redis.call('ZADD', KEYS[2], now + lease_ms, run_id)
redis.call('PEXPIRE', KEYS[2], lease_ms + 1000)
return {0, rpm + 1, active + 1}
"""

RELEASE_SCRIPT = """
return redis.call('ZREM', KEYS[1], ARGV[1])
"""


class RedisEvaluator(Protocol):
    async def eval(self, script: str, numkeys: int, *keys_and_args): ...


class AdmissionDenied(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class AdmissionUnavailable(RuntimeError):
    code = "GATEWAY_ADMISSION_UNAVAILABLE"


@dataclass(frozen=True)
class AdmissionResult:
    rpm: int
    active: int


def _keys(owner_id: uuid.UUID) -> tuple[str, str]:
    if not isinstance(owner_id, uuid.UUID):
        raise ValueError("配额归属必须是用户 UUID")
    tag = f"{{gateway:principal:{owner_id}}}"
    return f"gateway:{tag}:rpm", f"gateway:{tag}:leases"


async def acquire(redis: RedisEvaluator, owner_id: uuid.UUID, run_id: uuid.UUID,
                  rpm_limit: int, concurrent_limit: int,
                  lease_ms: int) -> AdmissionResult:
    """一次 Redis 脚本同时裁决滑动 60 秒 RPM 和未过期租约；异常时拒绝执行。"""
    if not isinstance(run_id, uuid.UUID):
        raise ValueError("Run 标识必须是 UUID")
    if (type(rpm_limit) is not int or not 1 <= rpm_limit <= 100_000
            or type(concurrent_limit) is not int or not 1 <= concurrent_limit <= 10_000
            or type(lease_ms) is not int or not 1_000 <= lease_ms <= 3_660_000):
        raise ValueError("配额或租约参数无效")
    keys = _keys(owner_id)
    try:
        result = await redis.eval(ACQUIRE_SCRIPT, 2, *keys, str(run_id),
                                  rpm_limit, concurrent_limit, lease_ms, WINDOW_MS)
        status, rpm, active = (int(item) for item in result)
    except Exception as exc:
        raise AdmissionUnavailable(AdmissionUnavailable.code) from exc
    if status:
        codes = {1: "GATEWAY_RPM_EXCEEDED", 2: "GATEWAY_CONCURRENCY_EXCEEDED",
                 3: "GATEWAY_RUN_ALREADY_ADMITTED"}
        raise AdmissionDenied(codes.get(status, "GATEWAY_ADMISSION_UNAVAILABLE"))
    return AdmissionResult(rpm, active)


async def release(redis: RedisEvaluator, owner_id: uuid.UUID, run_id: uuid.UUID) -> bool:
    """终态释放并发租约，RPM 记录保留到窗口结束；失败由 TTL 兜底恢复。"""
    if not isinstance(run_id, uuid.UUID):
        raise ValueError("Run 标识必须是 UUID")
    try:
        return bool(await redis.eval(RELEASE_SCRIPT, 1, _keys(owner_id)[1], str(run_id)))
    except Exception as exc:
        raise AdmissionUnavailable(AdmissionUnavailable.code) from exc
