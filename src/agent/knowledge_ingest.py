# 模块职责：校验上传文件、提取文本与页码，再生成有大小上限、可保留章节和表头的分块。
"""Bounded text extraction and chunking for shared knowledge documents."""

# 导入 hashlib，用于计算文件、配置与模型内容摘要。
import hashlib
# 导入 io，用于在内存中把上传字节包装成文件流。
import io
# 导入 os，用于读取环境变量并执行必要的文件系统操作。
import os
# 导入 re，用于识别关键词、标题、引用编号或合法配置值。
import re
# 导入 zipfile，用于检查 DOCX 压缩包并限制解压规模。
import zipfile
# 从 dataclasses 导入 dataclass，供下方相关数据结构与业务步骤。
from dataclasses import dataclass
# 从 pathlib 导入 PurePath，供下方按路径对象处理文件。
from pathlib import PurePath

# 从 docx 导入 Document，供下方相关数据结构与业务步骤。
from docx import Document
# 从 docx.table 导入 Table，供下方相关数据结构与业务步骤。
from docx.table import Table
# 从 docx.text.paragraph 导入 Paragraph，供下方相关数据结构与业务步骤。
from docx.text.paragraph import Paragraph
# 从 langchain_text_splitters 导入 RecursiveCharacterTextSplitter，供下方相关数据结构与业务步骤。
from langchain_text_splitters import RecursiveCharacterTextSplitter
# 从 pypdf 导入 PdfReader，供下方相关数据结构与业务步骤。
from pypdf import PdfReader

# 准备「上传文件字节上限」：按当前配置合并、累计或计算该值。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# 准备「提取正文的字符上限」：设置为 200000。
MAX_EXTRACTED_CHARS = 200_000
# 准备「PDF 页数上限」：设置为 100。
MAX_PDF_PAGES = 100
# 准备「单文档分块数量上限」：设置为 300。
MAX_CHUNKS = 300
# 准备「DOCX 解压后累计体积上限」：按当前配置合并、累计或计算该值。
MAX_DOCX_EXPANDED_BYTES = 50 * 1024 * 1024

# 准备「支持的扩展名到 MIME 类型的映射」：组织该步骤需要的结构化字段。
CONTENT_TYPES = {
    # 写入「.txt」字段，与同一条记录的其他字段共同组成结果。
    ".txt": "text/plain",
    # 写入「.md」字段，与同一条记录的其他字段共同组成结果。
    ".md": "text/markdown",
    # 写入「.markdown」字段，与同一条记录的其他字段共同组成结果。
    ".markdown": "text/markdown",
    # 写入「.docx」字段，与同一条记录的其他字段共同组成结果。
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    # 写入「.pdf」字段，与同一条记录的其他字段共同组成结果。
    ".pdf": "application/pdf",
# 结束当前参数列表或结构化数据，后续语句继续处理这一结果。
}


# 定义类型：用专门异常区分文件不可安全解析与其他基础设施故障。
class DocumentValidationError(ValueError):
    # 模块职责：校验上传文件、提取文本与页码，再生成有大小上限、可保留章节和表头的分块。
    """The upload cannot be safely or usefully indexed."""


# 声明不可变的数据类，构造后不允许随意修改字段，减少解析结果在流程中被意外改变。
@dataclass(frozen=True)
# 定义类型：不可变分块数据：正文、起始页码与章节路径。
class KnowledgeChunk:
    # 声明「片段原文」数据字段及其类型；此处不执行数据库查询。
    text: str
    # 声明「页码（无页码模型时为 0）」数据字段及其类型；此处不执行数据库查询。
    page: int  # 1-based for PDF; zero when a file has no page model.
    # 声明「章节路径」数据字段及其类型；此处不执行数据库查询。
    section_path: str = ""


# 声明不可变的数据类，构造后不允许随意修改字段，减少解析结果在流程中被意外改变。
@dataclass(frozen=True)
# 定义类型：不可变文档数据：名称、类型、大小、哈希、分块和原始字节。
class PreparedDocument:
    # 声明「原文件名称」数据字段及其类型；此处不执行数据库查询。
    filename: str
    # 声明「文件 MIME 类型」数据字段及其类型；此处不执行数据库查询。
    content_type: str
    # 声明「原文件字节数」数据字段及其类型；此处不执行数据库查询。
    size_bytes: int
    # 声明「原文件 SHA-256 摘要」数据字段及其类型；此处不执行数据库查询。
    sha256: str
    # 声明「最终可索引分块列表」数据字段及其类型；此处不执行数据库查询。
    chunks: list[KnowledgeChunk]
    # 声明「原始文件字节」数据字段及其类型；此处不执行数据库查询。
    raw: bytes = b""


# 定义函数：移除上传路径并校验文件名长度、控制字符和受支持后缀。
def _filename(value: str | None) -> str:
    # 准备「规范化名称」：去除边界空白。
    name = (value or "").replace("\\", "/").split("/")[-1].strip()
    # 拒绝空名、超长名和控制字符，防止异常文件名进入显示与元数据链路。
    if not name or len(name) > 255 or any(ord(char) < 32 for char in name):
        # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
        raise DocumentValidationError("文件名无效或过长")
    # 只允许当前解析器明确支持的文件后缀，不依据任意客户端 MIME 值选择解析方式。
    if PurePath(name).suffix.lower() not in CONTENT_TYPES:
        # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
        raise DocumentValidationError("只支持 TXT、Markdown、DOCX、PDF 文件")
    # 返回去掉目录部分、通过类型与长度检查的文件名。
    return name


# 定义函数：先限制 DOCX 压缩包规模，再按顺序提取段落、标题和 Markdown 表格。
def _docx_text(raw: bytes) -> str:
    # DOCX 本质为 ZIP；先检查容器格式再交给文档解析器。
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
        raise DocumentValidationError("DOCX 文件格式无效")
    # 以内存字节打开 DOCX ZIP 包，检查完成后自动关闭包句柄。
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        # 准备「DOCX ZIP 包条目」：读取 ZIP 包条目元数据。
        entries = archive.infolist()
        # 限制压缩包条目数和解压后总字节数，降低 DOCX 解压炸弹风险。
        if len(entries) > 2000 or sum(item.file_size for item in entries) > MAX_DOCX_EXPANDED_BYTES:
            # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
            raise DocumentValidationError("DOCX 解压后体积过大")
    # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
    try:
        # 准备「当前文档对象/文档标识」：从上传字节解析 DOCX 文档。
        doc = Document(io.BytesIO(raw))
        # 准备「提取出的段落/表格文本或当前切分结果」：建立有序的数据列表。
        parts = []
        # 逐项处理当前 DOCX 段落/表格，保持原文顺序。
        for item in doc.iter_inner_content():
            # 按实际对象类型处理 DOCX 段落或表格，而不是猜测文本格式。
            if isinstance(item, Paragraph):
                # 准备「DOCX 段落样式名」：根据条件选择当前值或回退值。
                style = item.style.name if item.style else ""
                # 准备「标题层级」：查找目标模式。
                level = re.search(r"Heading (\d)", style)
                # 把当前已处理的数据加入结果/缓冲列表，供后续步骤统一使用。
                parts.append(("#" * int(level[1]) + " " if level else "") + item.text)
            # 检查另一种输入类型或状态，避免不同格式进入错误的处理路径。
            elif isinstance(item, Table):
                # 准备「数据库行或文档表格行」：按条件逐项转换、过滤或整理数据。
                rows = [[cell.text.replace("|", "\\|").replace("\n", " ") for cell in row.cells] for row in item.rows]
                # 表格至少有一行时才生成表头及后续数据行。
                if rows:
                    # 把当前已处理的数据加入结果/缓冲列表，供后续步骤统一使用。
                    parts.append("| " + " | ".join(rows[0]) + " |")
                    # 把当前已处理的数据加入结果/缓冲列表，供后续步骤统一使用。
                    parts.append("| " + " | ".join("---" for _ in rows[0]) + " |")
                    # 把当前批次的数据追加到既有列表，保持处理顺序。
                    parts.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        # 按原文顺序连接段落与表格行，交给后续分块流程。
        return "\n".join(parts)
    # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
    except Exception as exc:
        # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
        raise DocumentValidationError("无法解析 DOCX 文件") from exc


# 定义函数：校验 PDF 并逐页提取文本，必要时按显式配置尝试本地 OCR。
def _pdf_pages(raw: bytes) -> list[tuple[int, str]]:
    # 检查 PDF 文件签名；仅有 .pdf 后缀不能证明内容确实是 PDF。
    if not raw.startswith(b"%PDF-"):
        # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
        raise DocumentValidationError("PDF 文件格式无效")
    # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
    try:
        # 准备「PDF 读取器」：从内存文件流打开 PDF。
        reader = PdfReader(io.BytesIO(raw), strict=False)
        # 当前流程不处理加密 PDF，避免在没有密码的情况下产生不完整提取结果。
        if reader.is_encrypted:
            # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
            raise DocumentValidationError("暂不支持加密 PDF")
        # 检查数量/长度是否达到边界，防止越界或超预算处理。
        if len(reader.pages) > MAX_PDF_PAGES:
            # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
            raise DocumentValidationError(f"PDF 最多支持 {MAX_PDF_PAGES} 页")
        # 准备「合并证据涉及的页码」：建立有序的数据列表。
        pages = []
        # 准备「已提取正文的累计字符数」：设置为 0。
        total = 0
        # 逐项处理当前 PDF 页面，同时保留从 1 开始的页码。
        for number, page in enumerate(reader.pages, start=1):
            # 准备「当前转换或提取的值」：组合多个必要条件进行判断。
            value = (page.extract_text(extraction_mode="layout") if os.getenv("KNOWLEDGE_PDF_LAYOUT") == "1"
                     # 继续组合本次操作的参数、条件或结果字段。
                     else page.extract_text()) or ""
            # 仅当本页未提取到正文且显式启用 OCR 时，才调用本地识别器。
            if not value.strip() and os.getenv("KNOWLEDGE_OCR") == "1":
                # 从 src.agent.knowledge_ocr 导入 ocr_page，供下方相关数据结构与业务步骤。
                from src.agent.knowledge_ocr import ocr_page
                # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
                try:
                    # 准备「当前转换或提取的值」：把指定 PDF 页渲染为本机临时图片，再调用有超时限制的 Tesseract 提取文字。
                    value = ocr_page(raw, number - 1)
                # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
                except ValueError as exc:
                    # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
                    raise DocumentValidationError(str(exc)) from exc
            # 累计更新「已提取正文的累计字符数」，供当前步骤的计数或边界判断使用。
            total += len(value)
            # 提取正文累计超出 20 万字符时停止，防止解析结果无限膨胀。
            if total > MAX_EXTRACTED_CHARS:
                # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
                raise DocumentValidationError("文档正文过长")
            # 把当前已处理的数据加入结果/缓冲列表，供后续步骤统一使用。
            pages.append((number, value))
        # 返回从 1 开始的 PDF 页码和该页提取文本，不丢失页码来源。
        return pages
    # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
    except DocumentValidationError:
        # 继续传播原始异常，保留异常类型和调用堆栈，不将其吞掉。
        raise
    # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
    except Exception as exc:
        # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
        raise DocumentValidationError("无法解析 PDF 文件") from exc


# 定义函数：识别代码围栏外的 Markdown 标题，将正文与章节路径一起输出。
def _sections(text):
    # 函数约定：识别代码围栏外的 Markdown 标题，将正文与章节路径一起输出。
    """Preserve Markdown/DOCX heading paths, without interpreting fenced code."""
    # 准备「(path, buffer, fenced)」：把本步骤相关值放在同一有序组合中。
    path, buffer, fenced = [], [], False
    # 逐项处理当前 正文物理行，维护当前章节和代码围栏状态。
    for line in text.splitlines():
        # 检测 Markdown 代码围栏，防止把代码里的 # 误当成章节标题。
        if line.lstrip().startswith(("```", "~~~")):
            # 切换代码围栏状态；围栏内部不参与标题识别。
            fenced = not fenced
        # 准备「当前行匹配出的 Markdown 标题」：根据条件选择当前值或回退值。
        heading = re.match(r"^(#{1,6})\s+(.+)$", line) if not fenced else None
        # 当前行是章节标题，先输出此前正文，再更新后续正文使用的标题路径。
        if heading:
            # 只有章节缓冲中存在行时才尝试输出该段内容。
            if buffer:
                # 逐块输出当前章节或表格内容；生成器无需一次性复制整份文档。
                yield " / ".join(path)[-200:], "\n".join(buffer)
            # 准备「标题层级」：读取数量或长度。
            level = len(heading[1])
            # 准备「当前路径/章节层级」：按当前配置合并、累计或计算该值。
            path = path[:level - 1] + [heading[2].strip()[:100]]
            # 准备「当前章节的正文行缓冲区」：建立有序的数据列表。
            buffer = []
        # 进入前一条件未满足时的分支，采用对应的回退处理。
        else:
            # 把当前已处理的数据加入结果/缓冲列表，供后续步骤统一使用。
            buffer.append(line)
    # 只有章节缓冲中存在行时才尝试输出该段内容。
    if buffer:
        # 逐块输出当前章节或表格内容；生成器无需一次性复制整份文档。
        yield " / ".join(path)[-200:], "\n".join(buffer)


# 定义函数：普通段落交给字符切分器；表格按行分批，后续块重复表头。
def _structured_parts(body, splitter):
    # 逐项处理当前 正文块或表格数据行，确保跨块仍有表头。
    for block in re.split(r"\n\s*\n", body):
        # 去除块前后换行再拆物理行，避免分节后的空行导致漏识别表头。
        lines = block.strip('\n').splitlines()
        # 检查数量/长度是否达到边界，防止越界或超预算处理。
        if len(lines) > 2 and "|" in lines[0] and re.match(r"^\s*\|?\s*:?-{3,}", lines[1]):
            # 把表头和 Markdown 分隔行一起保存，拆成多块时每块都重复这两行。
            header = "\n".join(lines[:2])
            # 准备「当前锁定的数据库记录/当前分块」：使用表格的表头和分隔行。
            current = header
            # 逐项处理当前 正文块或表格数据行，确保跨块仍有表头。
            for row in lines[2:]:
                # 单行连同表头已超出分块上限时明确拒绝，避免悄悄切断一条表格记录。
                if len(header) + len(row) > 800:
                    # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
                    raise DocumentValidationError("表格单行过长，请先拆分长行")
                # 当前表格块装不下下一行时先输出，下一块从同一表头重新开始。
                if len(current) + len(row) > 800:
                    # 逐块输出当前章节或表格内容；生成器无需一次性复制整份文档。
                    yield current
                    # 准备「当前锁定的数据库记录/当前分块」：使用表格的表头和分隔行。
                    current = header
                # 累计更新「当前锁定的数据库记录/当前分块」，供当前步骤的计数或边界判断使用。
                current += "\n" + row
            # 逐块输出当前章节或表格内容；生成器无需一次性复制整份文档。
            yield current
        # 进入前一条件未满足时的分支，采用对应的回退处理。
        else:
            # 逐块输出当前章节或表格内容；生成器无需一次性复制整份文档。
            yield from splitter.split_text(block)


# 定义函数：统一接收原始文件，执行类型解析、文本限制与分块，返回可索引文档。
def prepare_document(filename: str | None, raw: bytes) -> PreparedDocument:
    # 准备「规范化名称」：移除上传路径并校验文件名长度、控制字符和受支持后缀。
    name = _filename(filename)
    # 拒绝空文件及超过 10 MiB 的原始内容，在进一步解析前建立资源边界。
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
        raise DocumentValidationError("文件为空或超过 10 MB")
    # 准备「小写文件扩展名」：统一小写以便匹配。
    suffix = PurePath(name).suffix.lower()
    # PDF 使用按页提取，保留真实页码来源。
    if suffix == ".pdf":
        # 准备「合并证据涉及的页码」：校验 PDF 并逐页提取文本，必要时按显式配置尝试本地 OCR。
        pages = _pdf_pages(raw)
    # 检查另一种输入类型或状态，避免不同格式进入错误的处理路径。
    elif suffix == ".docx":
        # 准备「合并证据涉及的页码」：建立有序的数据列表。
        pages = [(0, _docx_text(raw))]
    # 进入前一条件未满足时的分支，采用对应的回退处理。
    else:
        # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
        try:
            # 准备「合并证据涉及的页码」：建立有序的数据列表。
            pages = [(0, raw.decode("utf-8-sig"))]
        # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
        except UnicodeDecodeError as exc:
            # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
            raise DocumentValidationError("文本文件必须使用 UTF-8 编码") from exc

    # 准备「有重叠的递归字符切分器」：配置递归字符切分器。
    splitter = RecursiveCharacterTextSplitter(
        # 设置「基础块长度上限」参数，使当前调用使用明确的配置或元数据。
        chunk_size=800,
        # 设置「相邻块重叠长度」参数，使当前调用使用明确的配置或元数据。
        chunk_overlap=120,
        # 设置「从段落到字符的切分边界」参数，使当前调用使用明确的配置或元数据。
        separators=["\n\n", "\n", "。", "！", "？", "，", " ", ""],
    # 结束当前参数列表或结构化数据，后续语句继续处理这一结果。
    )
    # 准备「最终可索引分块列表」：建立有序的数据列表。
    chunks = []
    # 准备「本次分块策略版本」：读取环境配置（未设置时采用代码中的默认值）。
    version = os.getenv("KNOWLEDGE_CHUNKING_VERSION", "2")
    # 只接受已实现的两种分块策略，避免把未知配置当作 v1 或 v2 使用。
    if version not in {"1", "2"}:
        # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
        raise DocumentValidationError("不支持的分块版本")
    # 准备「已提取正文的累计字符数」：设置为 0。
    total = 0
    # 逐项处理当前 页、章节和正文片段。
    for page, text in pages:
        # 拒绝含空字符的正文，避免数据库/解析链路对该字符的处理不一致。
        if "\x00" in text:
            # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
            raise DocumentValidationError("文档包含不可处理的空字符")
        # 累计更新「已提取正文的累计字符数」，供当前步骤的计数或边界判断使用。
        total += len(text)
        # 提取正文累计超出 20 万字符时停止，防止解析结果无限膨胀。
        if total > MAX_EXTRACTED_CHARS:
            # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
            raise DocumentValidationError("文档正文过长")
        # 准备「携带章节路径的正文块」：根据条件选择当前值或回退值。
        sections = _sections(text) if version == "2" else [("", text)]
        # 逐项处理当前 页、章节和正文片段。
        for section, body in sections:
            # 准备「提取出的段落/表格文本或当前切分结果」：根据条件选择当前值或回退值。
            parts = _structured_parts(body, splitter) if version == "2" else splitter.split_text(body)
            # 逐项处理当前 页、章节和正文片段。
            for part in parts:
                # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
                if part.strip():
                    # 准备「待匹配或保存的正文」：按当前配置合并、累计或计算该值。
                    content = (section + "\n\n" if section else "") + part.strip()
                    # 把当前已处理的数据加入结果/缓冲列表，供后续步骤统一使用。
                    chunks.append(KnowledgeChunk(content, page, section))
                    # 检查数量/长度是否达到边界，防止越界或超预算处理。
                    if len(chunks) > MAX_CHUNKS:
                        # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
                        raise DocumentValidationError("文档分块过多")
    # 没有任何可索引正文时拒绝入库；默认关闭 OCR 的扫描件也会走到这里。
    if not chunks:
        # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
        raise DocumentValidationError("没有提取到可检索文本；扫描版 PDF 请先进行 OCR")
    # 组装文档元数据、SHA-256、正文分块和原始字节，供归档与索引共用。
    return PreparedDocument(
        # 设置「原文件名称」参数，使当前调用使用明确的配置或元数据。
        filename=name,
        # 设置「文件 MIME 类型」参数，使当前调用使用明确的配置或元数据。
        content_type=CONTENT_TYPES[suffix],
        # 设置「原文件字节数」参数，使当前调用使用明确的配置或元数据。
        size_bytes=len(raw),
        # 设置「原文件 SHA-256 摘要」参数，使当前调用使用明确的配置或元数据。
        sha256=hashlib.sha256(raw).hexdigest(),
        # 设置「最终可索引分块列表」参数，使当前调用使用明确的配置或元数据。
        chunks=chunks,
        # 设置「原始文件字节」参数，使当前调用使用明确的配置或元数据。
        raw=raw,
    # 结束当前参数列表或结构化数据，后续语句继续处理这一结果。
    )
