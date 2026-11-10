# 模块职责：为本地模型配置和权重计算内容指纹，按文件大小与修改时间缓存计算结果。
"""Content fingerprints for local model files, cached by size and modification time."""
# 导入 hashlib，用于计算文件、配置与模型内容摘要。
import hashlib
# 从 functools 导入 lru_cache，供下方复用已初始化资源或内容指纹。
from functools import lru_cache
# 从 pathlib 导入 Path，供下方按路径对象处理文件。
from pathlib import Path


# 缓存指定数量的资源/文件签名计算结果，避免重复创建连接池或反复读取模型权重。
@lru_cache(maxsize=4)
# 定义函数：逐文件流式计算内容 SHA-256，再汇总成模型指纹，避免把大权重一次读入内存。
def _fingerprint(signature):
    # 准备「文件相对路径与内容摘要的映射」：组织该步骤需要的结构化字段。
    files = {}
    # 逐项处理当前 模型文件及其有序内容块。
    for name, size, modified, relative in signature:
        # 准备「增量 SHA-256 计算器」：计算 SHA-256 完整性摘要。
        digest = hashlib.sha256()
        # 使用上下文管理器打开资源，确保异常或提前返回时也能关闭句柄。
        with Path(name).open('rb') as handle:
            # 每次读取 1 MiB，直到文件结束；适用于体积较大的本地模型权重。
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                # 把这一小块文件内容加入 SHA-256 状态，内存中无需同时保留整个模型权重。
                digest.update(block)
        # 按相对路径保存内容摘要，避免不同子目录同名配置互相覆盖。
        files[relative] = digest.hexdigest()
    # 准备「当前转换或提取的值」：将内容摘要转换成十六进制字符串。
    value = hashlib.sha256(repr(sorted(files.items())).encode()).hexdigest()
    # 返回依据相对文件名与内容摘要构成的模型总指纹。
    return value


# 定义函数：收集模型配置与权重的文件属性，以此复用内容指纹计算缓存。
def embedding_version(path):
    # 准备「资源根路径」：构造文件系统路径对象。
    root = Path(path)
    # 收集模型 JSON 配置和权重文件；配置子目录也参与版本指纹。
    paths = set(root.rglob('*.json')) | set(root.glob('*.safetensors')) | set(root.glob('pytorch_model*.bin'))
    # 把有序文件路径、大小、纳秒修改时间及相对路径组成缓存键，文件改变后重新计算内容摘要。
    signature = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns, str(p.relative_to(root))) for p in sorted(paths))
    # 有本地文件才计算模型版本，否则显式标记文件不可用，不下载替代模型。
    return _fingerprint(signature) if signature else 'local-files-unavailable'
