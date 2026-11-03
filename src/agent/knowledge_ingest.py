"""Bounded text extraction and chunking for shared knowledge documents."""

import hashlib
import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePath

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_EXTRACTED_CHARS = 200_000
MAX_PDF_PAGES = 100
MAX_CHUNKS = 300
MAX_DOCX_EXPANDED_BYTES = 50 * 1024 * 1024

CONTENT_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
}


class DocumentValidationError(ValueError):
    """The upload cannot be safely or usefully indexed."""


@dataclass(frozen=True)
class KnowledgeChunk:
    text: str
    page: int  # 1-based for PDF; zero when a file has no page model.


@dataclass(frozen=True)
class PreparedDocument:
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    chunks: list[KnowledgeChunk]


def _filename(value: str | None) -> str:
    name = (value or "").replace("\\", "/").split("/")[-1].strip()
    if not name or len(name) > 255 or any(ord(char) < 32 for char in name):
        raise DocumentValidationError("文件名无效或过长")
    if PurePath(name).suffix.lower() not in CONTENT_TYPES:
        raise DocumentValidationError("只支持 TXT、Markdown、DOCX、PDF 文件")
    return name


def _docx_text(raw: bytes) -> str:
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        raise DocumentValidationError("DOCX 文件格式无效")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(item.file_size for item in entries) > MAX_DOCX_EXPANDED_BYTES:
            raise DocumentValidationError("DOCX 解压后体积过大")
    try:
        doc = Document(io.BytesIO(raw))
        parts = []
        for item in doc.iter_inner_content():
            if isinstance(item, Paragraph):
                parts.append(item.text)
            elif isinstance(item, Table):
                parts.extend(
                    "\t".join(cell.text for cell in row.cells) for row in item.rows
                )
        return "\n".join(parts)
    except Exception as exc:
        raise DocumentValidationError("无法解析 DOCX 文件") from exc


def _pdf_pages(raw: bytes) -> list[tuple[int, str]]:
    if not raw.startswith(b"%PDF-"):
        raise DocumentValidationError("PDF 文件格式无效")
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            raise DocumentValidationError("暂不支持加密 PDF")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentValidationError(f"PDF 最多支持 {MAX_PDF_PAGES} 页")
        pages = []
        total = 0
        for number, page in enumerate(reader.pages, start=1):
            value = page.extract_text() or ""
            total += len(value)
            if total > MAX_EXTRACTED_CHARS:
                raise DocumentValidationError("文档正文过长")
            pages.append((number, value))
        return pages
    except DocumentValidationError:
        raise
    except Exception as exc:
        raise DocumentValidationError("无法解析 PDF 文件") from exc


def prepare_document(filename: str | None, raw: bytes) -> PreparedDocument:
    name = _filename(filename)
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise DocumentValidationError("文件为空或超过 10 MB")
    suffix = PurePath(name).suffix.lower()
    if suffix == ".pdf":
        pages = _pdf_pages(raw)
    elif suffix == ".docx":
        pages = [(0, _docx_text(raw))]
    else:
        try:
            pages = [(0, raw.decode("utf-8-sig"))]
        except UnicodeDecodeError as exc:
            raise DocumentValidationError("文本文件必须使用 UTF-8 编码") from exc

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=120,
        separators=["\n\n", "\n", "。", "！", "？", "，", " ", ""],
    )
    chunks = []
    total = 0
    for page, text in pages:
        if "\x00" in text:
            raise DocumentValidationError("文档包含不可处理的空字符")
        total += len(text)
        if total > MAX_EXTRACTED_CHARS:
            raise DocumentValidationError("文档正文过长")
        for part in splitter.split_text(text):
            if part.strip():
                chunks.append(KnowledgeChunk(part.strip(), page))
                if len(chunks) > MAX_CHUNKS:
                    raise DocumentValidationError("文档分块过多")
    if not chunks:
        raise DocumentValidationError("没有提取到可检索文本；扫描版 PDF 请先进行 OCR")
    return PreparedDocument(
        filename=name,
        content_type=CONTENT_TYPES[suffix],
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        chunks=chunks,
    )
