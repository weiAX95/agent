# 模块职责：仅在显式启用时调用本机 Tesseract，限制页数、像素和执行时间，及时关闭渲染资源。
"""Opt-in local OCR with bounded rendering and no network dependency."""
# 导入 os，用于读取环境变量并执行必要的文件系统操作。
import os
# 导入 shutil，用于查找已安装的本机 OCR 命令。
import shutil
# 导入 subprocess，用于调用有时间限制的本机 Tesseract 进程。
import subprocess
# 导入 tempfile，用于创建私有临时文件或临时目录。
import tempfile
# 从 pathlib 导入 Path，供下方按路径对象处理文件。
from pathlib import Path


# 定义函数：把指定 PDF 页渲染为本机临时图片，再调用有超时限制的 Tesseract 提取文字。
def ocr_page(raw: bytes, page_index: int) -> str:
    # 先确认 Tesseract 已安装；缺少依赖时直接解释原因，不自动安装或联网下载。
    if not shutil.which('tesseract'):
        # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
        raise ValueError('本地 OCR 需要预先安装 Tesseract 及所选语言包')
    # 导入 pypdfium2（别名 pdfium），用于当前模块的基础操作。
    import pypdfium2 as pdfium
    # 以原始字节打开 PDF，退出上下文时自动关闭整个文档资源。
    with pdfium.PdfDocument(raw) as pdf:
        # 检查数量/长度是否达到边界，防止越界或超预算处理。
        if len(pdf) > 20:
            # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
            raise ValueError('本地 OCR 最多支持 20 页')
        # 准备「页码（无页码模型时为 0）」：从已取得的数据中读取对应字段/切片。
        page = pdf[page_index]
        # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
        try:
            # 二维放大 2 倍产生约 4 倍像素；渲染前限制总像素，防止异常 PDF 消耗过量内存。
            if page.get_width() * page.get_height() * 4 > 20_000_000:
                # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
                raise ValueError('OCR 页面像素超过上限')
            # 以 2 倍尺寸渲染当前 PDF 页，提高 OCR 可读性，之后由 finally 释放位图。
            bitmap = page.render(scale=2)
            # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
            try:
                # 只在临时目录保留 OCR 图片，退出时自动清理本页中间文件。
                with tempfile.TemporaryDirectory(prefix='knowledge-ocr-') as tmp:
                    # 准备「当前路径/章节层级」：按当前配置合并、累计或计算该值。
                    path = Path(tmp) / 'page.png'
                    # 只在私有临时目录保存当前页图片，供本机 OCR 命令读取。
                    bitmap.to_pil().save(path)
                    # 直接传递命令参数列表调用本机 OCR，不经过 shell 拼接，避免命令注入。
                    result = subprocess.run(['tesseract', str(path), 'stdout', '-l',
                        # 从服务端读取 OCR 语言组合，默认简体中文加英文；只作为单独参数传给本机程序。
                        os.getenv('KNOWLEDGE_OCR_LANG', 'chi_sim+eng')],
                        # 设置「捕获子进程标准输出和错误」参数，使当前调用使用明确的配置或元数据。
                        capture_output=True, text=True, timeout=30, check=False)
                    # 非零退出码表示本机 OCR 未成功，不能把错误输出当成正文。
                    if result.returncode:
                        # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
                        raise ValueError('本地 OCR 失败，请检查语言包和图片')
                    # 返回 OCR 标准输出中的识别文字；资源仍会按 finally 路径关闭。
                    return result.stdout
            # 无论成功、失败或提前返回，都执行下方资源清理/统计逻辑。
            finally:
                # 无论 OCR 成功还是失败都释放当前位图资源。
                bitmap.close()
        # 无论成功、失败或提前返回，都执行下方资源清理/统计逻辑。
        finally:
            # 最后释放 PDF 页句柄；外层 PDF 上下文再关闭整个文档。
            page.close()
