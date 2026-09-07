"""上传文件签名和 Office 压缩包边界测试。"""

import zipfile

import pytest

from app.config import Settings
from app.errors import KnowledgeUploadRequestError
from app.operations.uploads import validate_staged_file


def _settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


def test_rejects_extension_spoofed_pdf(tmp_path) -> None:
    path = tmp_path / "fake.pdf"
    path.write_bytes(b"this is not a pdf")

    with pytest.raises(KnowledgeUploadRequestError):
        validate_staged_file(path, "manual.pdf", _settings())


def test_accepts_structurally_valid_small_docx_archive(tmp_path) -> None:
    path = tmp_path / "manual.docx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("word/document.xml", "<document>安全说明</document>")

    validate_staged_file(path, "manual.docx", _settings())


def test_rejects_office_archive_with_unsafe_expansion_ratio(tmp_path) -> None:
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("word/document.xml", "0" * 100_000)

    with pytest.raises(KnowledgeUploadRequestError):
        validate_staged_file(
            path,
            "bomb.docx",
            _settings(knowledge_upload_max_compression_ratio=2),
        )


def test_rejects_archive_traversal_member(tmp_path) -> None:
    path = tmp_path / "traversal.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("xl/workbook.xml", "<workbook />")
        archive.writestr("../escape.txt", "bad")

    with pytest.raises(KnowledgeUploadRequestError):
        validate_staged_file(path, "traversal.xlsx", _settings())
