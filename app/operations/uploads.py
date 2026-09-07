"""安全落盘并预校验用户批量上传的知识文件。"""

import asyncio
import hashlib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from fastapi import UploadFile
from app.config import PROJECT_ROOT, Settings
from app.errors import KnowledgeUploadRequestError, KnowledgeUploadTooLargeError
from app.ingestion import SUPPORTED_EXTENSIONS, parse_source


READ_CHUNK_SIZE = 1024 * 1024
PDF_MAGIC = b"%PDF-"
OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
OFFICE_ARCHIVE_MARKERS = {
    ".docx": "word/document.xml",
    ".xlsx": "xl/workbook.xml",
    ".xlsm": "xl/workbook.xml",
}


@dataclass(frozen=True, slots=True)
class StagedKnowledgeFile:
    """一个已安全落盘并通过结构校验的上传文件。"""

    original_filename: str
    source_path: Path
    file_size_bytes: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class StagedKnowledgeBatch:
    """一次上传请求生成的文件批次。"""

    batch_id: str
    directory: Path
    files: list[StagedKnowledgeFile]


def _remove_staged_directory(directory: Path) -> None:
    """回滚当前请求创建的精确批次目录，不触碰其他上传。"""
    if not directory.exists():
        return
    for child in directory.iterdir():
        if child.is_file():
            child.unlink()
    directory.rmdir()


def _validate_office_archive(
    path: Path,
    suffix: str,
    settings: Settings,
) -> None:
    """限制 Office ZIP 的展开规模、压缩比、路径和必要结构。"""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > settings.knowledge_upload_max_archive_entries:
                raise KnowledgeUploadRequestError("unsafe office archive entry count")
            total_size = 0
            compressed_size = 0
            names: set[str] = set()
            for entry in entries:
                member = PurePosixPath(entry.filename.replace("\\", "/"))
                if member.is_absolute() or ".." in member.parts or entry.flag_bits & 0x1:
                    raise KnowledgeUploadRequestError("unsafe office archive member")
                total_size += max(0, entry.file_size)
                compressed_size += max(0, entry.compress_size)
                names.add(entry.filename.casefold())
            if total_size > settings.knowledge_upload_max_uncompressed_bytes:
                raise KnowledgeUploadRequestError("office archive expands beyond limit")
            ratio = total_size / max(1, compressed_size)
            if ratio > settings.knowledge_upload_max_compression_ratio:
                raise KnowledgeUploadRequestError("office archive compression ratio is unsafe")
            if (
                "[content_types].xml" not in names
                or OFFICE_ARCHIVE_MARKERS[suffix] not in names
            ):
                raise KnowledgeUploadRequestError("office archive structure is invalid")
    except (OSError, zipfile.BadZipFile) as exc:
        raise KnowledgeUploadRequestError("office archive is invalid") from exc


def validate_staged_file(path: Path, original_filename: str, settings: Settings) -> None:
    """在交给复杂解析器前验证文件签名并拦截常见压缩炸弹。"""
    suffix = Path(original_filename).suffix.casefold()
    with path.open("rb") as source:
        head = source.read(8192)
    if not head:
        raise KnowledgeUploadRequestError("empty knowledge file")
    if suffix == ".pdf" and not head.startswith(PDF_MAGIC):
        raise KnowledgeUploadRequestError("PDF signature is invalid")
    if suffix in OFFICE_ARCHIVE_MARKERS:
        _validate_office_archive(path, suffix, settings)
    elif suffix == ".xls" and not head.startswith(OLE_MAGIC):
        raise KnowledgeUploadRequestError("XLS signature is invalid")
    elif suffix == ".json":
        normalized = head.lstrip(b"\xef\xbb\xbf \t\r\n")
        if not normalized.startswith((b"{", b"[")):
            raise KnowledgeUploadRequestError("JSON signature is invalid")
    elif suffix in {".txt", ".md", ".markdown", ".html", ".htm"} and b"\x00" in head:
        raise KnowledgeUploadRequestError("text document contains binary data")


async def stage_knowledge_uploads(
    files: list[UploadFile],
    settings: Settings,
) -> StagedKnowledgeBatch:
    """流式保存多个知识文件，限制容量并在建任务前完成解析校验。"""
    if not files or len(files) > settings.knowledge_upload_max_files:
        raise KnowledgeUploadRequestError("invalid upload file count")

    allowed_root = (PROJECT_ROOT / "data" / "knowledge").resolve()
    upload_root = (allowed_root / "uploads").resolve()
    upload_root.mkdir(parents=True, exist_ok=True)

    batch_id = str(uuid4())
    batch_directory = (upload_root / batch_id).resolve()
    if batch_directory.parent != upload_root:
        raise KnowledgeUploadRequestError("invalid batch directory")
    batch_directory.mkdir(exist_ok=False)

    staged: list[StagedKnowledgeFile] = []
    seen_filenames: set[str] = set()
    batch_size = 0
    try:
        for upload in files:
            original_filename = Path(upload.filename or "").name.strip()
            if (
                not original_filename
                or len(original_filename) > 255
                or Path(original_filename).suffix.lower() not in SUPPORTED_EXTENSIONS
            ):
                raise KnowledgeUploadRequestError("unsupported knowledge file type")
            logical_filename = original_filename.casefold()
            if logical_filename in seen_filenames:
                raise KnowledgeUploadRequestError(
                    "duplicate logical filename in one upload batch"
                )
            seen_filenames.add(logical_filename)

            target = batch_directory / (
                f"{uuid4().hex}{Path(original_filename).suffix.lower()}"
            )
            digest = hashlib.sha256()
            file_size = 0
            try:
                with target.open("xb") as destination:
                    while chunk := await upload.read(READ_CHUNK_SIZE):
                        file_size += len(chunk)
                        batch_size += len(chunk)
                        if (
                            file_size > settings.knowledge_upload_max_file_bytes
                            or batch_size > settings.knowledge_upload_max_batch_bytes
                        ):
                            raise KnowledgeUploadTooLargeError()
                        digest.update(chunk)
                        destination.write(chunk)
            finally:
                await upload.close()

            try:
                await asyncio.to_thread(
                    validate_staged_file,
                    target,
                    original_filename,
                    settings,
                )
                await asyncio.wait_for(
                    asyncio.to_thread(
                        parse_source,
                        target,
                        original_filename=original_filename,
                        pdf_ocr_enabled=settings.pdf_ocr_enabled,
                        pdf_ocr_language=settings.pdf_ocr_language,
                        pdf_ocr_dpi=settings.pdf_ocr_dpi,
                        excel_rows_per_block=settings.excel_rows_per_block,
                    ),
                    timeout=settings.knowledge_upload_parse_timeout_seconds,
                )
            except (
                ImportError,
                OSError,
                TimeoutError,
                UnicodeDecodeError,
                ValueError,
            ) as exc:
                raise KnowledgeUploadRequestError(
                    "knowledge document could not be parsed"
                ) from exc

            staged.append(
                StagedKnowledgeFile(
                    original_filename=original_filename,
                    source_path=target,
                    file_size_bytes=file_size,
                    content_sha256=digest.hexdigest(),
                )
            )
    except Exception:
        for upload in files:
            await upload.close()
        _remove_staged_directory(batch_directory)
        raise

    return StagedKnowledgeBatch(
        batch_id=batch_id,
        directory=batch_directory,
        files=staged,
    )


def discard_staged_batch(batch: StagedKnowledgeBatch) -> None:
    """数据库任务创建失败时移除尚未交给后台任务的文件。"""
    _remove_staged_directory(batch.directory)
