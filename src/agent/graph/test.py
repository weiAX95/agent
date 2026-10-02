import operator
from typing import Annotated

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict


class State(TypedDict):
    """
    状态类型定义
    aggregate: 使用 operator.add reducer 使这个列表为追加模式，
    确保每个节点的结果都能被正确合并
    """  # The operator.add reducer fn makes this append-only

    aggregate: Annotated[list, operator.add]


def a(state: State):
    """
    节点 a：启动分支
    此节点是工作流的起点，负责初始化流程并分发到不同的分支。Args:
    state: 当前状态
    Returns:
    dict: 包含新结果的状态更新
    """
    print(f'Adding "A" to {state["aggregate"]}')
    return {"aggregate": ["A"]}


def b(state: State):
    """
    节点 b：第一个分支
    此节点处理第一个分支的任务，与节点 c 并行执行。
    Args:
    state: 当前状态
    Returns:
    dict: 包含新结果的状态更新
    """
    print(f'Adding "B" to {state["aggregate"]}')
    return {"aggregate": ["B"]}


def b_2(state: State):
    """
    节点 b2：第二个分支
    此节点处理第二个分支的任务，在节点 b 完成后执行。
    Args:
    state: 当前状态
    Returns:
    dict: 包含新结果的状态更新
    """
    print(f'Adding "B_2" to {state["aggregate"]}')
    return {"aggregate": ["B_2"]}


def c(state: State):
    """节点 c：另一个分支
    此节点处理另一个分支的任务，与节点 b 并行执行。
    Args:
    state: 当前状态
    Returns:
    dict: 包含新结果的状态更新
    """
    print(f'Adding "C" to {state["aggregate"]}')
    return {"aggregate": ["C"]}


def d(state: State):
    """
    节点 d：延迟执行的汇总节点
    此节点设置了 defer=True，因此会等待所有其他任务完成后才执行。它负责汇总所有分支的结果。
    Args:
    state: 当前状态
    Returns:
    dict: 包含新结果的状态更新
    """
    print(f'Adding "D" to {state["aggregate"]}')
    return {"aggregate": ["D"]}


# 创建图
builder = StateGraph(State)
# 添加节点
builder.add_node("a", a)
builder.add_node("b", b)
builder.add_node("b_2", b_2)
builder.add_node("c", c)
builder.add_node("d", d, defer=True)  # 设置 defer=True 延迟执行
# 添加边
builder.add_edge(START, "a")
builder.add_edge("a", "b")
builder.add_edge("a", "c")
builder.add_edge("b", "b_2")
builder.add_edge("b_2", "d")
builder.add_edge("c", "d")
builder.add_edge("d", END)
# 编译图
graph = builder.compile()
# 执行图
print("=== 开始执行工作流 ===")
result = graph.invoke({})
print("=== 执行结果 ===")
print(result)
