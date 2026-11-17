"""Agent Runtime 单次执行的领域约束，不直接访问数据库或模型服务。

本模块是 Run Controller 的第一阶段契约，只能在进程内约束一次执行。
持久化状态、跨 Worker 协调以及实际 Graph 接入将在后续阶段完成。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class RunStatus(StrEnum):
    """一次 Agent Run 在当前阶段允许的状态。"""

    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class RunStateError(RuntimeError):
    """Run 的当前状态不允许执行目标操作。"""


class RunLimitError(RuntimeError):
    """Run 的执行边界已触发，code 供上层转换为稳定错误码。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RunLimits:
    """一个 Run 的静态约束；后续 token/费用预算属于独立的计量阶段。"""

    max_steps: int = 24
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        # 禁止不受限制的运行，同时拒绝 bool 等看似整数的错误配置。
        if type(self.max_steps) is not int or self.max_steps <= 0:
            raise ValueError("max_steps 必须是正整数")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise ValueError("timeout_seconds 必须为正数")
        if not 0 < self.timeout_seconds <= 3600:
            raise ValueError("timeout_seconds 必须在 (0, 3600] 秒内")


@dataclass(slots=True)
class RunController:
    """单次请求独占的内存状态机；不能跨进程共享或作为持久化真相。"""

    run_id: uuid.UUID
    request_id: uuid.UUID
    owner_id: uuid.UUID
    limits: RunLimits = field(default_factory=RunLimits)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    status: RunStatus = field(default=RunStatus.CREATED, init=False)
    _started_at: float | None = field(default=None, init=False, repr=False)
    _steps: int = field(default=0, init=False, repr=False)

    @property
    def steps(self) -> int:
        """已获许可的调度步数。"""

        return self._steps

    def start(self) -> None:
        """开始计时；禁止重复启动同一个 Run。"""

        if self.status != RunStatus.CREATED:
            raise RunStateError("Run 只能启动一次")
        self._started_at = self.clock()
        self.status = RunStatus.RUNNING

    def check_active(self) -> None:
        """在真正安排新工作前核对状态和总时限。"""

        if self.status != RunStatus.RUNNING:
            raise RunStateError(f"Run 当前状态不可执行：{self.status}")
        assert self._started_at is not None
        # 使用单调时钟避免系统时间调整导致超时策略失效。
        if self.clock() - self._started_at >= self.limits.timeout_seconds:
            self.status = RunStatus.TIMED_OUT
            raise RunLimitError("RUN_DEADLINE_EXCEEDED")

    def before_step(self) -> int:
        """在调度一步 Agent 工作之前申请许可，超过上限则立即终止。"""

        self.check_active()
        if self._steps >= self.limits.max_steps:
            self.status = RunStatus.FAILED
            raise RunLimitError("RUN_STEP_LIMIT_EXCEEDED")
        self._steps += 1
        return self._steps

    def finish(self, status: RunStatus) -> None:
        """只接受一次终态转换，禁止将未确认的结果重复标记为完成。"""

        if status not in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
        }:
            raise ValueError("finish 只接受终态")
        if self.status != RunStatus.RUNNING:
            raise RunStateError(f"Run 已不可结束：{self.status}")
        if status == RunStatus.SUCCEEDED:
            # 成功落点必须再次检查截止时间，防止迟到的结果被算作成功。
            self.check_active()
        self.status = status
