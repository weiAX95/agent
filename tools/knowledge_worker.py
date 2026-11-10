# 模块职责：提供执行单个任务、持续领取任务和预览/执行清理的命令行入口。
"""Explicit worker and reconciliation commands for existing knowledge services."""
# 导入 argparse，用于声明并解析 worker 的命令行参数。
import argparse
# 导入 json，用于序列化结构化数据或构造安全的字符串字面量。
import json
# 导入 sys，用于处理模块搜索路径和命令行退出状态。
import sys
# 导入 time，用于以单调时钟统计耗时或安排轮询间隔。
import time
# 导入 uuid，用于验证 UUID 或生成稳定/随机标识。
import uuid
# 从 pathlib 导入 Path，供下方按路径对象处理文件。
from pathlib import Path
# 准备「项目根目录」：从已取得的数据中读取对应字段/切片。
ROOT = Path(__file__).resolve().parents[1]
# 将项目根目录放到模块搜索路径前部，支持直接运行命令行文件。
sys.path.insert(0, str(ROOT))


# 定义函数：解析 worker 命令，按 once/work/reconcile 执行，并在退出时释放数据库资源。
def main(argv=None):
    # 准备「命令行参数解析器」：建立命令行参数解析器。
    parser = argparse.ArgumentParser(description=__doc__)
    # 声明一个 worker 命令行选项，输入类型与默认行为在此固定。
    parser.add_argument('command', choices=['once','work','reconcile'])
    # 声明一个 worker 命令行选项，输入类型与默认行为在此固定。
    parser.add_argument('--job-id', type=uuid.UUID)
    # 声明一个 worker 命令行选项，输入类型与默认行为在此固定。
    parser.add_argument('--apply', action='store_true', help='Actually purge inactive revisions (default: dry run)')
    # 声明一个 worker 命令行选项，输入类型与默认行为在此固定。
    parser.add_argument('--scan-vectors', action='store_true', help='Also inventory legacy vectors without job/catalog records')
    # 准备「解析完成的命令行选项」：解析命令行输入。
    args = parser.parse_args(argv)
    # 从 dotenv 导入 load_dotenv，供下方相关数据结构与业务步骤。
    from dotenv import load_dotenv
    # 加载项目 .env；现有环境变量优先，连接与凭证不写死在代码中。
    load_dotenv(ROOT / '.env')
    # 从 src.agent.knowledge_database 导入 knowledge_engine, close_knowledge_database，供下方相关数据结构与业务步骤。
    from src.agent.knowledge_database import knowledge_engine, close_knowledge_database
    # 从 src.agent.knowledge_jobs 导入 run_job, reconcile，供下方相关数据结构与业务步骤。
    from src.agent.knowledge_jobs import run_job, reconcile
    # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
    try:
        # 准备「可复用的同步 SQL 引擎」：延迟创建并缓存同步 PostgreSQL 引擎，限制连接、等待和语句执行时间。
        engine = knowledge_engine()
        # 根据操作员显式指定的命令参数选择执行路径。
        if args.command == 'reconcile':
            # 向命令行输出结构化任务结果，方便操作员观察或脚本收集。
            print(json.dumps(reconcile(engine, apply=args.apply, scan_vectors=args.scan_vectors)))
            # 以退出码 0 表示本轮命令正常完成或由用户正常停止。
            return 0
        # 准备「最近一次周期清理的单调时间」：读取不受系统时钟校准影响的计时值。
        last_cleanup = time.monotonic()
        # 持续处理下一轮任务、候选窗口或盘点批次，直到内部退出条件成立。
        while True:
            # 准备「任务/处理状态」：领取任务并在事务外解析/写向量；最后重新加锁校验租约，决定发布、冲突或失败。
            status = run_job(engine, args.job_id)
            # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
            if status:
                # 向命令行输出结构化任务结果，方便操作员观察或脚本收集。
                print(json.dumps({'status':status}), flush=True)
            # 根据操作员显式指定的命令参数选择执行路径。
            if args.command == 'once':
                # 单次模式下，失败、冲突或失去租约返回退出码 1；其余情况返回 0。
                return 1 if status in {'failed','conflict','lease_lost'} else 0
            # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
            if time.monotonic() - last_cleanup >= 300:
                # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
                try:
                    # 向命令行输出结构化任务结果，方便操作员观察或脚本收集。
                    print(json.dumps({'reconciliation':reconcile(engine, apply=True)}), flush=True)
                # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
                except Exception as exc:
                    # 向命令行输出结构化任务结果，方便操作员观察或脚本收集。
                    print(json.dumps({'reconciliation_error':type(exc).__name__}), flush=True)
                # 准备「最近一次周期清理的单调时间」：读取不受系统时钟校准影响的计时值。
                last_cleanup = time.monotonic()
            # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
            if status is None:
                # 暂无任务时等待 2 秒，避免空队列忙轮询占满 CPU。
                time.sleep(2)
    # 允许操作员用 Ctrl+C 正常停止持续 worker，退出前仍执行资源释放。
    except KeyboardInterrupt:
        # 以退出码 0 表示本轮命令正常完成或由用户正常停止。
        return 0
    # 无论成功、失败或提前返回，都执行下方资源清理/统计逻辑。
    finally:
        # 仅关闭已经初始化的连接池，并清除引擎缓存，避免关闭时反而新建连接。
        close_knowledge_database()


# 只在直接执行本模块时启动 CLI；普通导入不会开始轮询或执行清理。
if __name__ == '__main__':
    # 执行命令行入口并把返回值转换成进程退出码，供操作员或进程管理器识别。
    raise SystemExit(main())
