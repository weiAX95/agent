"""A model-callable way to publish a user-visible analysis summary."""

from typing import Literal

from langchain_core.tools import StructuredTool


def report_progress(
    summary: str,
    phase: Literal["理解问题", "查证资料", "权衡方案", "形成回答"] | None = None,
    basis: str = "",
    next_step: str = "",
) -> str:
    """Show a concise public summary of the current phase, evidence, and next action."""
    return "进度已展示给用户；请继续处理并给出最终回答。"


progress_tool = StructuredTool.from_function(
    func=report_progress,
    name="report_progress",
    description=(
        "向用户实时展示公开的分析过程摘要。复杂任务在理解问题、查证资料、权衡方案、形成回答等阶段调用，"
        "每轮通常 2 到 4 次。phase 选择当前阶段；summary 用一到两句话说清当前判断或处理方向；"
        "basis 仅写已取得的证据或用户明确给出的约束；next_step 写下一步要做什么。"
        "不要输出内部逐步推理、隐私、工具原文或未核实结论。"
        "简单问题可直接回答，不需要调用。"
    ),
)
